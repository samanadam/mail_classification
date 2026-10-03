from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.api.deps import current_user, get_db
from mailcat.models import Category, Correction, Mail, Rule, Tombstone, User
from mailcat.worker.policy import plan_deletions

router = APIRouter(prefix="/api/categories", tags=["categories"])

EDITABLE = ("name", "description", "move_days", "mailbox_delete_days", "local_delete_days",
            "writeback_enabled", "model_enabled", "color")
DELETE_TIMERS = ("mailbox_delete_days", "local_delete_days")


def _out(c: Category, counts: dict[int, tuple[int, int]]) -> dict[str, Any]:
    total, unread = counts.get(c.id, (0, 0))
    return {
        "id": c.id, "name": c.name, "description": c.description, "position": c.position,
        "system_key": c.system_key, "move_days": c.move_days, "mailbox_delete_days": c.mailbox_delete_days,
        "local_delete_days": c.local_delete_days, "writeback_enabled": c.writeback_enabled,
        "model_enabled": c.model_enabled, "color": c.color, "mail_count": total, "unread_count": unread,
    }


@router.get("")
def list_categories(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[dict]:
    rows = db.execute(select(Mail.category_id, func.count(), func.count().filter(Mail.seen.is_(False)))
                      .where(Mail.folder_role != "sent").group_by(Mail.category_id)).all()
    counts = {cid: (total, unread) for cid, total, unread in rows if cid is not None}
    return [_out(c, counts) for c in db.scalars(select(Category).order_by(Category.position, Category.id))]


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    move_days: int = Field(default=1, ge=0, le=3650)
    mailbox_delete_days: int | None = Field(default=None, ge=0, le=36500)
    local_delete_days: int | None = Field(default=None, ge=0, le=36500)
    writeback_enabled: bool = True
    model_enabled: bool = True
    color: str | None = Field(default=None, max_length=16)


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    move_days: int | None = Field(default=None, ge=0, le=3650)
    mailbox_delete_days: int | None = Field(default=None, ge=0, le=36500)
    local_delete_days: int | None = Field(default=None, ge=0, le=36500)
    writeback_enabled: bool | None = None
    model_enabled: bool | None = None
    color: str | None = Field(default=None, max_length=16)
    # Set after the user has seen the impact of a shorter delete timer.
    confirm_impact: bool = False


def _clean_name(db: Session, name: str, exclude_id: int | None = None) -> str:
    name = " ".join(name.split())
    if not name or any(ch in name for ch in "/\\|*%\"") or name.startswith("."):
        raise HTTPException(400, "category names cannot contain / \\ | * % \" or start with a dot")
    clash = db.scalar(select(Category).where(func.lower(Category.name) == name.lower()))
    if clash is not None and clash.id != exclude_id:
        raise HTTPException(409, f"a category named {name} already exists")
    return name


@router.post("")
def create_category(body: CategoryIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    name = _clean_name(db, body.name)
    position = (db.scalar(select(func.max(Category.position))) or 0) + 1
    cat = Category(**{**body.model_dump(), "name": name}, position=position)
    db.add(cat)
    db.flush()
    settings_store.log_change(db, f"category:{cat.id}", None, {"created": name}, user.username)
    return _out(cat, {})


def impact(db: Session, category: Category, values: dict[str, int | None]) -> dict[str, Any]:
    """How many mails the next run would delete with these timers, versus now."""
    settings = settings_store.get_all(db)
    now = datetime.now(UTC)
    current = [p for p in plan_deletions(db, settings, now) if p.category.id == category.id]
    proposed = [p for p in plan_deletions(db, settings, now, category_overrides={category.id: values})
                if p.category.id == category.id]

    def summarize(plans: list) -> dict[str, int]:
        return {
            "mailbox": sum(1 for p in plans if p.kind == "mailbox"),
            "local": sum(1 for p in plans if p.kind == "local"),
            "live": len({(p.account.id, p.message_id) for p in plans if p.mode == "live"}),
        }

    before, after = summarize(current), summarize(proposed)
    return {"before": before, "after": after, "additional": max(0, after["live"] - before["live"]),
            "additional_total": max(0, after["mailbox"] + after["local"] - before["mailbox"] - before["local"])}


@router.get("/{category_id}/impact")
def category_impact(category_id: int, mailbox_delete_days: int | None = None,
                    local_delete_days: int | None = None, db: Session = Depends(get_db),
                    _: User = Depends(current_user)) -> dict:
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "category not found")
    return impact(db, category, {"mailbox_delete_days": mailbox_delete_days,
                                 "local_delete_days": local_delete_days})


def _shortens(old: int | None, new: int | None) -> bool:
    return new is not None and (old is None or new < old)


@router.patch("/{category_id}")
def update_category(category_id: int, body: CategoryPatch, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> dict:
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "category not found")
    changes = body.model_dump(exclude_unset=True)
    changes.pop("confirm_impact", None)
    if "name" in changes and changes["name"] is not None:
        changes["name"] = _clean_name(db, changes["name"], category.id)
    if category.system_key == "undecided" and changes.get("model_enabled"):
        raise HTTPException(400, "the model never chooses Undecided")
    for key in ("move_days", "name", "writeback_enabled", "model_enabled"):
        if key in changes and changes[key] is None:
            raise HTTPException(400, f"{key} cannot be empty")

    new_timers = {k: changes.get(k, getattr(category, k)) for k in DELETE_TIMERS}
    if any(_shortens(getattr(category, k), new_timers[k]) for k in DELETE_TIMERS if k in changes):
        result = impact(db, category, new_timers)
        if not body.confirm_impact:
            raise HTTPException(409, {"message": "this change makes more mail eligible for deletion",
                                      "impact": result})
    for key, value in changes.items():
        old = getattr(category, key)
        if old != value:
            setattr(category, key, value)
            settings_store.log_change(db, f"category:{category.id}.{key}", old, value, user.username)
    return _out(category, {})


class Reorder(BaseModel):
    ids: list[int]


@router.post("/reorder")
def reorder(body: Reorder, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    cats = {c.id: c for c in db.scalars(select(Category))}
    if set(body.ids) != set(cats):
        raise HTTPException(400, "send every category id exactly once")
    for pos, cid in enumerate(body.ids):
        cats[cid].position = pos
    settings_store.log_change(db, "categories.order", None, body.ids, user.username)
    return {"ok": True}


class Merge(BaseModel):
    into_id: int


@router.post("/{category_id}/merge")
def merge(category_id: int, body: Merge, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    source, target = db.get(Category, category_id), db.get(Category, body.into_id)
    if source is None or target is None or source.id == target.id:
        raise HTTPException(400, "pick two different categories")
    if source.system_key:
        raise HTTPException(400, f"{source.name} is a built-in category and cannot be merged away")
    moved = db.execute(update(Mail).where(Mail.category_id == source.id).values(
        category_id=target.id, decision_detail=f"merged from {source.name}")).rowcount
    db.execute(update(Tombstone).where(Tombstone.category_id == source.id).values(category_id=target.id))
    db.execute(update(Correction).where(Correction.new_category_id == source.id).values(
        new_category_id=target.id))
    db.execute(update(Rule).where(Rule.category_id == source.id).values(category_id=target.id))
    for rule in db.scalars(select(Rule)):
        ids = [int(i) for i in (rule.candidate_category_ids or [])]
        if source.id in ids:
            rule.candidate_category_ids = sorted({target.id if i == source.id else i for i in ids})
    settings_store.log_change(db, f"category:{source.id}", {"name": source.name},
                              {"merged_into": target.name, "mails": moved}, user.username)
    db.delete(source)
    return {"ok": True, "mails_moved": moved}


@router.delete("/{category_id}")
def delete_category(category_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "category not found")
    if category.system_key:
        raise HTTPException(400, "built-in categories cannot be deleted")
    if db.scalar(select(func.count()).select_from(Mail).where(Mail.category_id == category.id)):
        raise HTTPException(409, "the category still has mail; merge it into another category instead")
    settings_store.log_change(db, f"category:{category.id}", {"name": category.name}, None, user.username)
    db.delete(category)
    return {"ok": True}
