"""Reverse the moves (and Trash deletes) of one run: `mailcat undo <run_id>`."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailcat.imap.client import ImapError
from mailcat.models import Account, ActionLog, Mail, Run, Tombstone
from mailcat.worker.accounts import inbox_folder, open_session
from mailcat.worker.run import run_lock
from mailcat.worker.writeback import MoveResult, move_and_verify


def undo_run(session: Session, run_id: int, include_deletes: bool = True, dry_run: bool = False,
             triggered_by: str | None = None) -> dict[str, int]:
    """Moves every verified move/delete of `run_id` back, newest first. Undone mail is held in
    place (write-back leaves it alone) until its category is changed in the dashboard."""
    actions = ["move", "delete"] if include_deletes else ["move"]
    entries = list(session.scalars(select(ActionLog).where(
        ActionLog.run_id == run_id, ActionLog.action.in_(actions), ActionLog.dry_run.is_(False),
        ActionLog.undone_at.is_(None), ActionLog.error.is_(None),
    ).order_by(ActionLog.id.desc())))
    counts = {"undone": 0, "failed": 0, "skipped": 0}
    if not entries:
        return counts
    with run_lock():
        undo_run_row = Run(kind="undo", triggered_by=triggered_by, status="running",
                           counts={"undo_of": run_id})
        session.add(undo_run_row)
        session.flush()
        errors: list[dict] = []
        by_account: dict[int, list[ActionLog]] = {}
        for entry in entries:
            by_account.setdefault(entry.account_id or 0, []).append(entry)
        for account_id, account_entries in by_account.items():
            account = session.get(Account, account_id)
            if account is None:
                counts["skipped"] += len(account_entries)
                continue
            with open_session(session, account) as imap:
                for entry in account_entries:
                    if not entry.message_id or not entry.from_folder or not entry.to_folder:
                        counts["skipped"] += 1
                        continue
                    if dry_run:
                        counts["undone"] += 1
                        continue
                    try:
                        result = move_and_verify(imap, entry.message_id, None, entry.to_folder, entry.from_folder)
                    except ImapError as exc:
                        result = MoveResult("failed", error=str(exc))
                    now = datetime.now(UTC)
                    session.add(ActionLog(
                        run_id=undo_run_row.id, account_id=account.id, mail_id=entry.mail_id,
                        message_id=entry.message_id, action=f"undo_{entry.action}",
                        from_folder=entry.to_folder, to_folder=entry.from_folder, rule=f"undo of run {run_id}",
                        sender=entry.sender, subject=entry.subject, mail_date=entry.mail_date,
                        verified=result.state == "moved" and result.error is None, error=result.error,
                    ))
                    if result.state != "moved":
                        counts["failed" if result.state == "failed" else "skipped"] += 1
                        errors.append({"step": "undo", "message": f"{entry.message_id}: {result.error}"})
                        continue
                    entry.undone_at = now
                    counts["undone"] += 1
                    mail = session.scalar(select(Mail).where(Mail.account_id == account.id,
                                                             Mail.message_id == entry.message_id))
                    inbox = inbox_folder(imap, account)
                    role = "inbox" if entry.from_folder.upper() == inbox.upper() else "sorted"
                    if mail is not None:
                        mail.folder, mail.folder_role = entry.from_folder, role
                        mail.uid, mail.uidvalidity = result.new_uid, result.uidvalidity
                        mail.hold_writeback = True
                        if entry.action == "delete":
                            mail.mailbox_deleted_at = None
                            mail.keep = True
                    else:
                        tomb = session.scalar(select(Tombstone).where(
                            Tombstone.account_id == account.id, Tombstone.message_id == entry.message_id))
                        if tomb is not None:
                            tomb.folder, tomb.folder_role, tomb.uid = entry.from_folder, role, result.new_uid
                            tomb.mailbox_deleted_at = None
                            tomb.keep = True
                    session.commit()
        undo_run_row.status = "partial" if errors else "ok"
        undo_run_row.finished_at = datetime.now(UTC)
        undo_run_row.counts = {"undo_of": run_id, **counts}
        undo_run_row.errors = errors
        session.commit()
    return counts
