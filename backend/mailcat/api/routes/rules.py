from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.api.deps import current_user, get_db
from mailcat.classify.engine import features_of
from mailcat.classify.rules import FIELDS, OPS, RuleError, condition_matches, validate_conditions
from mailcat.models import Account, Category, Mail, Rule, User

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _out(r: Rule) -> dict[str, Any]:
    return {"id": r.id, "name": r.name, "position": r.position, "enabled": r.enabled,
            "conditions": r.conditions, "action": r.action, "category_id": r.category_id,
            "candidate_category_ids": r.candidate_category_ids or []}


@router.get("")
def list_rules(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    return {"rules": [_out(r) for r in db.scalars(select(Rule).order_by(Rule.position, Rule.id))],
            "fields": list(FIELDS) + ["header:<name>"], "ops": list(OPS)}


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    conditions: list[dict[str, Any]]
    action: Literal["assign", "restrict"] = "assign"
    category_id: int | None = None
    candidate_category_ids: list[int] = Field(default_factory=list)
    position: int | None = None


def _validate(db: Session, body: RuleIn) -> list[dict[str, Any]]:
    try:
        conditions = validate_conditions(body.conditions)
    except RuleError as exc:
        raise HTTPException(400, str(exc)) from exc
    ids = set(db.scalars(select(Category.id)))
    if body.action == "assign":
        if body.category_id not in ids:
            raise HTTPException(400, "pick the category this rule assigns")
    else:
        if len(body.candidate_category_ids) < 2 or not set(body.candidate_category_ids) <= ids:
            raise HTTPException(400, "a restrict rule needs at least two candidate categories")
    return conditions


@router.post("")
def create_rule(body: RuleIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    conditions = _validate(db, body)
    position = body.position if body.position is not None else (db.scalar(select(func.max(Rule.position))) or 0) + 10
    rule = Rule(name=body.name, enabled=body.enabled, conditions=conditions, action=body.action,
                category_id=body.category_id if body.action == "assign" else None,
                candidate_category_ids=body.candidate_category_ids if body.action == "restrict" else [],
                position=position)
    db.add(rule)
    db.flush()
    settings_store.log_change(db, f"rule:{rule.id}", None, _out(rule), user.username)
    return _out(rule)


@router.put("/{rule_id}")
def update_rule(rule_id: int, body: RuleIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    rule = db.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    conditions = _validate(db, body)
    old = _out(rule)
    rule.name, rule.enabled, rule.conditions, rule.action = body.name, body.enabled, conditions, body.action
    rule.category_id = body.category_id if body.action == "assign" else None
    rule.candidate_category_ids = body.candidate_category_ids if body.action == "restrict" else []
    if body.position is not None:
        rule.position = body.position
    settings_store.log_change(db, f"rule:{rule.id}", old, _out(rule), user.username)
    return _out(rule)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    rule = db.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    settings_store.log_change(db, f"rule:{rule.id}", _out(rule), None, user.username)
    db.delete(rule)
    return {"ok": True}


class RuleTest(BaseModel):
    conditions: list[dict[str, Any]]
    sample: int = Field(default=2000, ge=1, le=20000)


@router.post("/test")
def test_rule(body: RuleTest, db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    """How many of the most recent mails the conditions match, with a few examples."""
    try:
        conditions = validate_conditions(body.conditions)
    except RuleError as exc:
        raise HTTPException(400, str(exc)) from exc
    names = dict(db.execute(select(Account.id, Account.name)).all())
    mails = db.scalars(select(Mail).where(Mail.folder_role != "sent").order_by(Mail.received_at.desc())
                       .limit(body.sample))
    matched = []
    total = 0
    for mail in mails:
        total += 1
        feats = features_of(mail, names.get(mail.account_id, ""))
        if all(condition_matches(feats, c) for c in conditions):
            matched.append(mail)
    return {"checked": total, "matched": len(matched), "examples": [
        {"id": m.id, "from_address": m.from_address, "subject": m.subject, "category_id": m.category_id}
        for m in matched[:15]]}
