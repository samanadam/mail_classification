"""Per-sender delete timers ("delete everything from this shop after 3 days")."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.api.deps import current_user, get_db
from mailcat.models import SenderTimer, User
from mailcat.worker.policy import normalize_sender_pattern, plan_deletions

router = APIRouter(prefix="/api/sender-timers", tags=["sender-timers"])


def _out(t: SenderTimer) -> dict[str, Any]:
    return {"id": t.id, "pattern": t.pattern, "mailbox_delete_days": t.mailbox_delete_days,
            "local_delete_days": t.local_delete_days, "enabled": t.enabled, "note": t.note}


class SenderTimerIn(BaseModel):
    pattern: str = Field(min_length=3, max_length=320)
    mailbox_delete_days: int | None = Field(default=None, ge=0, le=36500)
    local_delete_days: int | None = Field(default=None, ge=0, le=36500)
    enabled: bool = True
    note: str = Field(default="", max_length=500)
    confirm_impact: bool = False


def _live(plans: list) -> set[tuple[int, str]]:
    return {(p.account.id, p.message_id) for p in plans if p.mode == "live"}


def _check_impact(db: Session, proposed: list[SenderTimer], confirm: bool) -> dict[str, int]:
    """Refuse (409) a change that makes more mail deletable, until the user confirms."""
    settings = settings_store.get_all(db)
    now = datetime.now(UTC)
    current = list(db.scalars(select(SenderTimer).where(SenderTimer.enabled.is_(True))))
    before = plan_deletions(db, settings, now, sender_timers=current)
    after = plan_deletions(db, settings, now, sender_timers=proposed)
    added = _live(after) - _live(before)
    impact = {"additional": len(added), "planned_total": len(after), "live_total": len(_live(after))}
    if added and not confirm:
        raise HTTPException(409, {"message": "this change makes more mail eligible for deletion",
                                  "impact": impact})
    return impact


@router.get("")
def list_timers(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[dict]:
    return [_out(t) for t in db.scalars(select(SenderTimer).order_by(SenderTimer.pattern))]


def _transient(body: SenderTimerIn, pattern: str) -> SenderTimer:
    return SenderTimer(pattern=pattern, mailbox_delete_days=body.mailbox_delete_days,
                       local_delete_days=body.local_delete_days, enabled=body.enabled, note=body.note)


@router.post("")
def create_timer(body: SenderTimerIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    try:
        pattern = normalize_sender_pattern(body.pattern)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.mailbox_delete_days is None and body.local_delete_days is None:
        raise HTTPException(400, "set at least one timer")
    if db.scalar(select(SenderTimer).where(SenderTimer.pattern == pattern)):
        raise HTTPException(409, f"a timer for {pattern} already exists")
    current = list(db.scalars(select(SenderTimer).where(SenderTimer.enabled.is_(True))))
    _check_impact(db, [*current, _transient(body, pattern)], body.confirm_impact)
    timer = _transient(body, pattern)
    db.add(timer)
    db.flush()
    settings_store.log_change(db, f"sender_timer:{timer.id}", None, _out(timer), user.username)
    return _out(timer)


@router.put("/{timer_id}")
def update_timer(timer_id: int, body: SenderTimerIn, db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> dict:
    timer = db.get(SenderTimer, timer_id)
    if timer is None:
        raise HTTPException(404, "sender timer not found")
    try:
        pattern = normalize_sender_pattern(body.pattern)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.mailbox_delete_days is None and body.local_delete_days is None:
        raise HTTPException(400, "set at least one timer, or delete the sender timer")
    clash = db.scalar(select(SenderTimer).where(SenderTimer.pattern == pattern))
    if clash is not None and clash.id != timer.id:
        raise HTTPException(409, f"a timer for {pattern} already exists")
    others = [t for t in db.scalars(select(SenderTimer).where(SenderTimer.enabled.is_(True))) if t.id != timer.id]
    _check_impact(db, [*others, _transient(body, pattern)], body.confirm_impact)
    old = _out(timer)
    timer.pattern, timer.mailbox_delete_days, timer.local_delete_days = (
        pattern, body.mailbox_delete_days, body.local_delete_days)
    timer.enabled, timer.note = body.enabled, body.note
    settings_store.log_change(db, f"sender_timer:{timer.id}", old, _out(timer), user.username)
    return _out(timer)


@router.delete("/{timer_id}")
def delete_timer(timer_id: int, confirm_impact: bool = False, db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> dict:
    timer = db.get(SenderTimer, timer_id)
    if timer is None:
        raise HTTPException(404, "sender timer not found")
    # Removing a sender timer can hand mail back to a shorter category timer.
    others = [t for t in db.scalars(select(SenderTimer).where(SenderTimer.enabled.is_(True))) if t.id != timer.id]
    settings = settings_store.get_all(db)
    now = datetime.now(UTC)
    added = _live(plan_deletions(db, settings, now, sender_timers=others)) - _live(
        plan_deletions(db, settings, now))
    if added and not confirm_impact:
        raise HTTPException(409, {"message": "without this sender timer, category timers would delete more mail",
                                  "impact": {"additional": len(added)}})
    settings_store.log_change(db, f"sender_timer:{timer.id}", _out(timer), None, user.username)
    db.delete(timer)
    return {"ok": True}
