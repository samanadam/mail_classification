"""Step 6: delete from the mailbox (move to Trash) and/or locally when timers expire.

Safeguards: Trash only (never EXPUNGE outside the move fallback), Message-ID checks before and
after, Gmail All Mail check, protected mail, daily cap, per-account switch with dry-run, a log
line per deletion, tombstones for local deletes.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from mailcat.imap.client import ImapError, ImapSession
from mailcat.ingest import attachments as attachment_store
from mailcat.models import Account, ActionLog, Correction, Mail, Tombstone
from mailcat.worker.context import RunContext
from mailcat.worker.policy import PlannedDeletion
from mailcat.worker.writeback import MoveResult, move_and_verify

log = logging.getLogger(__name__)


def _log(session: Session, ctx: RunContext, plan: PlannedDeletion, action: str, src: str | None,
         dst: str | None, dry_run: bool, verified: bool | None = None, error: str | None = None) -> None:
    mail = plan.mail
    session.add(ActionLog(
        run_id=ctx.run_id, account_id=plan.account.id, mail_id=mail.id if mail and action != "local_delete" else None,
        message_id=plan.message_id, action=action, from_folder=src, to_folder=dst, rule=plan.rule,
        sender=mail.from_address if mail else None, subject=(mail.subject or "")[:500] if mail else None,
        mail_date=plan.received_at, dry_run=dry_run, verified=verified, error=error,
    ))


def delete_from_mailbox(session: Session, imap: ImapSession, account: Account, plans: list[PlannedDeletion],
                        ctx: RunContext, now: datetime | None = None) -> set[str]:
    """Executes mailbox plans for one account. Returns Message-IDs that failed."""
    now = now or datetime.now(UTC)
    failed: set[str] = set()
    trash = imap.resolve_role("trash")
    all_mail = imap.resolve_role("all") if imap.is_gmail else None
    if trash is None:
        ctx.error("retention", "no Trash folder found; nothing deleted (set folder_overrides.trash)",
                  account.name)
        return {p.message_id for p in plans}
    if imap.is_gmail and all_mail is None:
        ctx.error("retention", "Gmail All Mail folder not found; cannot verify deletes, nothing deleted",
                  account.name)
        return {p.message_id for p in plans}

    for plan in plans:
        item = plan.mail or plan.tombstone
        assert item is not None
        source = item.folder
        if plan.mode == "dry_run":
            _log(session, ctx, plan, "delete", source, trash, dry_run=True)
            ctx.count("deletes_planned_dry_run")
            continue
        if not source:
            continue
        try:
            result = move_and_verify(imap, plan.message_id, item.uid, source, trash)
            if result.state == "moved" and all_mail and imap.search_message_id(all_mail, plan.message_id):
                result.error = "verify failed: Gmail still keeps it in All Mail"
        except ImapError as exc:
            result = MoveResult("failed", error=str(exc))
        verified = result.state == "moved" and result.error is None
        _log(session, ctx, plan, "delete", source, trash, dry_run=False, verified=verified, error=result.error)
        if result.state == "skipped":
            failed.add(plan.message_id)
            ctx.error("retention", f"{plan.message_id}: {result.error}", account.name, alert=False)
            continue
        if result.state == "failed":
            failed.add(plan.message_id)
            ctx.error("verify", f"delete {plan.message_id} from {source}: {result.error}", account.name)
            item.uid = None
            continue
        if result.error:
            ctx.error("verify", f"delete {plan.message_id}: {result.error}", account.name)
        item.mailbox_deleted_at = now
        item.folder_role = "trash"
        item.folder = trash
        item.uid = result.new_uid
        if isinstance(item, Mail):
            item.uidvalidity = result.uidvalidity
        ctx.count("deleted_mailbox")
        session.flush()
    return failed


def delete_local(session: Session, plans: list[PlannedDeletion], ctx: RunContext, skip: set[str]) -> None:
    for plan in plans:
        mail = plan.mail
        if mail is None or mail.message_id in skip:
            continue
        if plan.mode == "dry_run":
            _log(session, ctx, plan, "local_delete", mail.folder, None, dry_run=True)
            ctx.count("local_deletes_planned_dry_run")
            continue
        _log(session, ctx, plan, "local_delete", mail.folder, None, dry_run=False, verified=True)
        session.add(Tombstone(
            account_id=mail.account_id, message_id=mail.message_id, folder_role=mail.folder_role,
            folder=mail.folder, uid=mail.uid, uidvalidity=mail.uidvalidity, received_at=mail.received_at,
            category_id=mail.category_id, flagged=mail.flagged, keep=mail.keep,
            mailbox_deleted_at=mail.mailbox_deleted_at,
        ))
        # Corrections keep their labels for statistics but lose the mail content.
        session.execute(update(Correction).where(Correction.mail_id == mail.id).values(features=None))
        stored = [a.stored_name for a in mail.attachments]
        session.delete(mail)
        session.flush()
        for name in stored:
            attachment_store.remove(name)
        ctx.count("deleted_local")
