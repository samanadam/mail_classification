"""Step 5: move inbox mail past its grace period into its category folder.

Never touches mail outside INBOX and our own category folders. Every move is checked by
Message-ID before it happens and verified (location and flags) after.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailcat.imap.client import ImapError, ImapSession
from mailcat.models import Account, ActionLog, Category, FolderState, Mail
from mailcat.worker.context import RunContext
from mailcat.worker.policy import effective_timer, mode

log = logging.getLogger(__name__)

KEPT_FLAGS = ("\\Seen", "\\Flagged")


@dataclass
class PlannedMove:
    mail: Mail
    category: Category
    reason: str


def plan_moves(session: Session, account: Account, categories: dict[int, Category], now: datetime,
               limit: int) -> list[PlannedMove]:
    plans: list[PlannedMove] = []
    mails = session.scalars(select(Mail).where(
        Mail.account_id == account.id, Mail.folder_role.in_(("inbox", "sorted")),
        Mail.category_id.is_not(None), Mail.hold_writeback.is_(False),
        Mail.classified_at.is_not(None),
    ).order_by(Mail.received_at))
    for mail in mails:
        category = categories.get(mail.category_id)  # type: ignore[arg-type]
        if category is None or not category.writeback_enabled:
            continue
        if mail.folder_role == "inbox":
            if mail.moved_at is not None:
                continue  # we moved it before and the user put it back: leave it
            days = effective_timer(account, category, "move_days")
            if days is None or mail.received_at > now - timedelta(days=days):
                continue
            plans.append(PlannedMove(mail, category, f"{category.name}: move after {days} day(s)"))
        else:
            plans.append(PlannedMove(mail, category, f"category changed to {category.name}"))
        if len(plans) >= limit:
            break
    return plans


def _log(session: Session, ctx: RunContext, account: Account, mail: Mail, action: str, src: str | None,
         dst: str | None, rule: str, dry_run: bool, verified: bool | None = None,
         error: str | None = None) -> ActionLog:
    entry = ActionLog(
        run_id=ctx.run_id, account_id=account.id, mail_id=mail.id, message_id=mail.message_id,
        action=action, from_folder=src, to_folder=dst, rule=rule, sender=mail.from_address,
        subject=(mail.subject or "")[:500], mail_date=mail.received_at, dry_run=dry_run, verified=verified,
        error=error,
    )
    session.add(entry)
    return entry


def reconcile_category_folders(session: Session, imap: ImapSession, account: Account,
                               categories: dict[int, Category], ctx: RunContext, live: bool) -> None:
    """Rename category folders after a category rename."""
    states = session.scalars(select(FolderState).where(
        FolderState.account_id == account.id, FolderState.role == "sorted",
        FolderState.category_id.is_not(None)))
    for state in states:
        category = categories.get(state.category_id)  # type: ignore[arg-type]
        if category is None:
            continue
        expected = imap.category_folder(account.sorted_parent, category.name)
        if state.folder == expected or not imap.folder_exists(state.folder) or imap.folder_exists(expected):
            continue
        entry = ActionLog(run_id=ctx.run_id, account_id=account.id, action="folder_rename",
                          from_folder=state.folder, to_folder=expected, rule="category renamed",
                          dry_run=not live)
        session.add(entry)
        if not live:
            continue
        try:
            imap.rename_folder(state.folder, expected)
        except ImapError as exc:
            entry.error = str(exc)
            entry.verified = False
            ctx.error("writeback", str(exc), account.name)
            continue
        entry.verified = imap.folder_exists(expected)
        for mail in session.scalars(select(Mail).where(Mail.account_id == account.id,
                                                       Mail.folder == state.folder)):
            mail.folder = expected
        state.folder = expected
        ctx.count("folders_renamed")


@dataclass
class MoveResult:
    state: str  # moved | skipped | failed
    new_uid: int | None = None
    uidvalidity: int | None = None
    error: str | None = None


def move_and_verify(imap: ImapSession, message_id: str, uid: int | None, source: str,
                    destination: str) -> MoveResult:
    """Move one mail, identified by Message-ID, and verify location and flags afterwards.

    skipped: nothing was changed (the mail was not where we expected it).
    failed:  the server state after the move does not match; details in `error`.
    """
    current = imap.search_message_id(source, message_id)
    if uid is None or uid not in current:
        if len(current) == 1:
            uid = current[0]
        else:
            return MoveResult("skipped", error=f"not found in {source}; left untouched")
    before = {r.uid: r for r in imap.fetch_meta(source, [uid])}
    flags_before = before[uid].flags if uid in before else None
    new_uid = imap.move(source, uid, destination)
    in_dest = imap.search_message_id(destination, message_id)
    still_in_source = uid in imap.search_message_id(source, message_id)
    uidvalidity = imap.uidvalidity(destination)
    if still_in_source:
        return MoveResult("failed", error=f"verify failed: still present in {source} after the move")
    if not in_dest:
        return MoveResult("failed", error=f"verify failed: not present in {destination} after the move")
    if new_uid is None or new_uid not in in_dest:
        new_uid = max(in_dest)
    result = MoveResult("moved", new_uid, uidvalidity)
    after = {r.uid: r for r in imap.fetch_meta(destination, [new_uid])}
    if flags_before is not None and new_uid in after:
        want = {f for f in flags_before if f in KEPT_FLAGS}
        got = {f for f in after[new_uid].flags if f in KEPT_FLAGS}
        if want != got:
            result.error = f"verify failed: flags changed from {sorted(want)} to {sorted(got)}"
    return result


def run_writeback(session: Session, imap: ImapSession, account: Account, categories: dict[int, Category],
                  ctx: RunContext, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    account_mode = mode(account.writeback_enabled, account.writeback_dry_run)
    if account_mode == "off":
        return
    live = account_mode == "live"
    reconcile_category_folders(session, imap, account, categories, ctx, live)
    for plan in plan_moves(session, account, categories, now, int(ctx.settings["move_batch_limit"])):
        mail = plan.mail
        source = mail.folder or "INBOX"
        destination = imap.category_folder(account.sorted_parent, plan.category.name)
        if source == destination:
            continue
        if not live:
            _log(session, ctx, account, mail, "move", source, destination, plan.reason, dry_run=True)
            ctx.count("moves_planned_dry_run")
            continue
        try:
            if imap.ensure_folder(destination):
                session.add(ActionLog(run_id=ctx.run_id, account_id=account.id, action="folder_create",
                                      to_folder=destination, rule=plan.reason, verified=True))
            if session.scalar(select(FolderState).where(FolderState.account_id == account.id,
                                                        FolderState.folder == destination)) is None:
                session.add(FolderState(account_id=account.id, folder=destination, role="sorted",
                                        category_id=plan.category.id, high_uid=0))
            result = move_and_verify(imap, mail.message_id, mail.uid, source, destination)
        except ImapError as exc:
            result = MoveResult("failed", error=str(exc))
        _log(session, ctx, account, mail, "move", source, destination, plan.reason, False,
             verified=result.state == "moved" and result.error is None, error=result.error)
        if result.state == "skipped":
            ctx.count("moves_skipped")
            ctx.error("writeback", f"{mail.message_id}: {result.error}", account.name, alert=False)
        elif result.state == "failed":
            ctx.error("verify", f"move {mail.message_id} {source} -> {destination}: {result.error}",
                      account.name)
            # Location is uncertain now; the next sync re-locates it by Message-ID.
            mail.uid = None
        else:
            if result.error:
                ctx.error("verify", f"move {mail.message_id}: {result.error}", account.name)
            mail.folder = destination
            mail.folder_role = "sorted"
            mail.uid = result.new_uid
            mail.uidvalidity = result.uidvalidity
            mail.moved_at = now
            ctx.count("moved")
        session.flush()
