from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.api.deps import current_user, get_db
from mailcat.models import Account, ActionLog, Category, Correction, Mail, Run, RunRequest, SettingsLog, User
from mailcat.security import blocklists
from mailcat.worker.policy import live_delete_count, plan_deletions
from mailcat.worker.scheduler import next_run_at

router = APIRouter(prefix="/api", tags=["runs"])


def _run_out(r: Run) -> dict[str, Any]:
    return {"id": r.id, "kind": r.kind, "triggered_by": r.triggered_by, "started_at": r.started_at,
            "finished_at": r.finished_at, "status": r.status, "counts": r.counts or {},
            "errors": r.errors or [], "heartbeat_at": r.heartbeat_at}


@router.get("/status")
def status(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    last = db.scalar(select(Run).where(Run.kind != "undo").order_by(Run.id.desc()).limit(1))
    running = db.scalar(select(Run).where(Run.status == "running").order_by(Run.id.desc()).limit(1))
    pending = db.scalar(select(RunRequest).where(RunRequest.picked_at.is_(None)).limit(1))
    settings = settings_store.get_all(db)
    accounts = [{"id": a.id, "name": a.name, "last_sync_at": a.last_sync_at, "last_sync_ok": a.last_sync_ok,
                 "last_sync_error": a.last_sync_error, "sync_enabled": a.sync_enabled}
                for a in db.scalars(select(Account).order_by(Account.id))]
    return {
        "last_run": _run_out(last) if last else None,
        "running": _run_out(running) if running else None,
        "sync_requested": pending is not None,
        "next_run_at": next_run_at(settings, datetime.now(UTC)),
        "runs_paused": settings["runs_paused"],
        "accounts": accounts,
        "blocklists": blocklists.status(),
        "review_count": db.scalar(select(func.count()).select_from(Mail).join(
            Category, Category.id == Mail.category_id).where(Category.system_key == "undecided")) or 0,
    }


@router.post("/runs/sync")
def sync_now(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    pending = db.scalar(select(RunRequest).where(RunRequest.picked_at.is_(None)).limit(1))
    if pending is None:
        db.add(RunRequest(requested_by=user.username))
    return {"ok": True, "queued": True}


@router.get("/runs")
def list_runs(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db),
              _: User = Depends(current_user)) -> list[dict]:
    return [_run_out(r) for r in db.scalars(select(Run).order_by(Run.id.desc()).limit(limit))]


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    summary = db.execute(select(ActionLog.action, ActionLog.dry_run, ActionLog.verified, func.count())
                         .where(ActionLog.run_id == run_id)
                         .group_by(ActionLog.action, ActionLog.dry_run, ActionLog.verified)).all()
    return {**_run_out(run), "actions": [
        {"action": a, "dry_run": d, "verified": v, "count": c} for a, d, v, c in summary]}


@router.get("/deletions/preview")
def deletion_preview(db: Session = Depends(get_db), _: User = Depends(current_user),
                     limit: int = Query(500, ge=1, le=5000)) -> dict:
    settings = settings_store.get_all(db)
    plans = plan_deletions(db, settings, datetime.now(UTC))
    items = []
    for p in plans[:limit]:
        mail = p.mail
        items.append({
            "kind": p.kind, "mode": p.mode, "account_id": p.account.id, "account_name": p.account.name,
            "category_id": p.category.id, "category_name": p.category.name, "rule": p.rule,
            "message_id": p.message_id, "received_at": p.received_at,
            "mail_id": mail.id if mail else None,
            "from_address": mail.from_address if mail else None,
            "subject": mail.subject if mail else "(local copy already deleted)",
        })
    live = live_delete_count(plans)
    cap = int(settings["daily_delete_cap"])
    return {"items": items, "total": len(plans), "live_count": live, "cap": cap, "over_cap": live > cap}


@router.get("/activity")
def activity(kind: Literal["actions", "settings"] = "actions", action: str | None = None,
             run_id: int | None = None, account_id: int | None = None, errors_only: bool = False,
             page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
             db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    if kind == "settings":
        q = select(SettingsLog).order_by(SettingsLog.id.desc())
        total = db.scalar(select(func.count()).select_from(SettingsLog)) or 0
        rows = db.scalars(q.offset((page - 1) * page_size).limit(page_size))
        return {"total": total, "items": [{"id": r.id, "key": r.key, "old_value": r.old_value,
                                           "new_value": r.new_value, "changed_by": r.changed_by,
                                           "created_at": r.created_at} for r in rows]}
    conds = []
    if action:
        conds.append(ActionLog.action == action)
    if run_id is not None:
        conds.append(ActionLog.run_id == run_id)
    if account_id is not None:
        conds.append(ActionLog.account_id == account_id)
    if errors_only:
        conds.append(ActionLog.error.is_not(None))
    total = db.scalar(select(func.count()).select_from(ActionLog).where(*conds)) or 0
    rows = db.scalars(select(ActionLog).where(*conds).order_by(ActionLog.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    return {"total": total, "items": [{
        "id": r.id, "run_id": r.run_id, "account_id": r.account_id, "mail_id": r.mail_id, "action": r.action,
        "from_folder": r.from_folder, "to_folder": r.to_folder, "rule": r.rule, "sender": r.sender,
        "subject": r.subject, "mail_date": r.mail_date, "dry_run": r.dry_run, "verified": r.verified,
        "error": r.error, "undone_at": r.undone_at, "created_at": r.created_at,
    } for r in rows]}


class Reclassify(BaseModel):
    scope: Literal["all", "undecided", "category"] = "undecided"
    category_id: int | None = None


@router.post("/reclassify")
def reclassify(body: Reclassify, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    """Queue mail for re-classification at the next run. Corrected mail is never changed."""
    conds = [Mail.category_locked.is_(False), Mail.folder_role != "sent"]
    if body.scope == "undecided":
        conds.append(Mail.category_id == select(Category.id).where(
            Category.system_key == "undecided").scalar_subquery())
    elif body.scope == "category":
        if body.category_id is None:
            raise HTTPException(400, "category_id is required")
        conds.append(Mail.category_id == body.category_id)
    count = db.execute(update(Mail).where(*conds).values(classified_at=None)).rowcount
    settings_store.log_change(db, "reclassify", None, {"scope": body.scope, "category_id": body.category_id,
                                                       "mails": count}, user.username)
    return {"queued": count}


@router.get("/stats")
def stats(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    by_source = dict(db.execute(select(Mail.decided_by, func.count()).where(Mail.folder_role != "sent")
                                .group_by(Mail.decided_by)).all())
    corrections = db.execute(select(Correction.source, func.count()).group_by(Correction.source)).all()
    return {
        "mails": db.scalar(select(func.count()).select_from(Mail)) or 0,
        "by_decision": {str(k): v for k, v in by_source.items()},
        "corrections": {k: v for k, v in corrections},
        "malicious": db.scalar(select(func.count()).select_from(Mail).where(Mail.malicious.is_(True))) or 0,
        "needs_action": db.scalar(select(func.count()).select_from(Mail).where(
            Mail.needs_action.is_(True), Mail.seen.is_(False))) or 0,
    }
