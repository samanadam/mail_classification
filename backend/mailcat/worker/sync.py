"""Step 2-3 for one account: fetch new mail (peek mode), store it, scan attachments, notice
mail moved or deleted by hand.

Two phases per account:
1. Snapshot every synced folder: UIDVALIDITY, all UIDs with flags (one FETCH 1:* FLAGS), and
   UID/flags/date/Message-ID for UIDs we have not seen yet.
2. Reconcile against the database by Message-ID: relocate known mail, detect drag-and-drop
   corrections into category folders, mark vanished mail as missing, import the rest.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailcat.corrections import record_correction
from mailcat.imap.client import FetchRecord, ImapSession
from mailcat.ingest import attachments as attachment_store
from mailcat.ingest.clamav import Scanner
from mailcat.ingest.parser import parse_message
from mailcat.models import Account, Attachment, Category, FolderState, Link, Mail, Tombstone
from mailcat.worker.context import RunContext

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class FolderSnapshot:
    name: str
    role: str
    state: FolderState
    uidvalidity: int
    flags: dict[int, tuple[str, ...]]
    new: list[FetchRecord] = field(default_factory=list)
    reset: bool = False


def _clean(text: str | None) -> str:
    return (text or "").replace("\x00", "")


def _synced_folders(session: Session, imap: ImapSession, account: Account,
                    categories: list[Category]) -> list[tuple[str, str, int | None]]:
    folders: list[tuple[str, str, int | None]] = [("INBOX", "inbox", None)]
    for role in ("sent", "spam"):
        name = imap.resolve_role(role)
        if name:
            folders.append((name, role, None))
    seen = {f[0] for f in folders}
    for cat in categories:
        path = imap.category_folder(account.sorted_parent, cat.name)
        if path not in seen and imap.folder_exists(path):
            folders.append((path, "sorted", cat.id))
            seen.add(path)
    # Folders of merged or deleted categories still hold mail we filed; keep tracking them.
    for state in session.scalars(select(FolderState).where(
            FolderState.account_id == account.id, FolderState.role == "sorted")):
        if state.folder not in seen and imap.folder_exists(state.folder):
            folders.append((state.folder, "sorted", state.category_id))
            seen.add(state.folder)
    return folders


def _state(session: Session, account: Account, folder: str, role: str, category_id: int | None) -> FolderState:
    state = session.scalar(select(FolderState).where(
        FolderState.account_id == account.id, FolderState.folder == folder))
    if state is None:
        state = FolderState(account_id=account.id, folder=folder, role=role, category_id=category_id,
                            high_uid=0)
        session.add(state)
        session.flush()
    state.role = role
    if category_id is not None:
        state.category_id = category_id
    return state


def snapshot_folder(imap: ImapSession, account: Account, state: FolderState, settings: dict,
                    ctx: RunContext) -> FolderSnapshot:
    folder, role = state.folder, state.role
    uidvalidity, _count = imap.select(folder, readonly=True)
    flags = imap.fetch_flags(folder)
    all_uids = sorted(flags)
    snap = FolderSnapshot(folder, role, state, uidvalidity, flags)

    first_time = state.uidvalidity is None
    if not first_time and state.uidvalidity != uidvalidity:
        log.warning("%s/%s: UIDVALIDITY changed, re-matching by Message-ID", account.name, folder)
        snap.reset = True
        state.high_uid = 0
        state.backfill_low_uid = None
        state.backfill_done = False
    state.uidvalidity = uidvalidity

    limit = int(settings["fetch_limit_per_folder"])
    max_uid = all_uids[-1] if all_uids else 0
    if role == "spam":
        since = datetime.now(UTC) - timedelta(days=int(settings["spam_lookback_days"]))
        recent = set(imap.uids_since(folder, since))
        new_uids = [u for u in all_uids if u > state.high_uid and u in recent]
    elif first_time and role in ("inbox", "sent"):
        # The first sync only takes recent mail; older history comes in through backfill.
        since = datetime.now(UTC) - timedelta(days=int(settings["initial_sync_days"]))
        new_uids = imap.uids_since(folder, since)
        state.backfill_low_uid = new_uids[0] if new_uids else max_uid + 1
    else:
        new_uids = [u for u in all_uids if u > state.high_uid]

    if len(new_uids) > limit:
        ctx.count("fetch_deferred", len(new_uids) - limit)
        new_uids = new_uids[:limit]
        state.high_uid = new_uids[-1]
    else:
        state.high_uid = max(max_uid, state.high_uid if not snap.reset else 0)

    backfill: list[int] = []
    if account.backfill_enabled and role in ("inbox", "sent") and not state.backfill_done:
        low = state.backfill_low_uid if state.backfill_low_uid is not None else (
            new_uids[0] if new_uids else max_uid + 1)
        older = [u for u in all_uids if u < low]
        batch = older[-int(settings["backfill_batch_size"]):]
        backfill = sorted(batch, reverse=True)  # newest first
        if batch:
            state.backfill_low_uid = batch[0]
        if len(older) <= len(batch):
            state.backfill_done = True
        ctx.count("backfill_fetched", len(backfill))

    wanted = sorted(set(new_uids) | set(backfill))
    if wanted:
        pause = int(settings["backfill_pause_ms"]) / 1000 if backfill else 0
        chunk = 200
        for start in range(0, len(wanted), chunk):
            snap.new.extend(imap.fetch_meta(folder, wanted[start:start + chunk]))
            if pause:
                time.sleep(pause)
    state.last_synced_at = datetime.now(UTC)
    return snap


def _locate(mail: Mail, snap: FolderSnapshot, rec: FetchRecord) -> None:
    mail.folder = snap.name
    mail.folder_role = snap.role
    mail.uid = rec.uid
    mail.uidvalidity = snap.uidvalidity
    mail.missing_since = None
    mail.seen = rec.seen
    mail.flagged = rec.flagged


def sync_account(session: Session, imap: ImapSession, account: Account, categories: list[Category],
                 scanner: Scanner, ctx: RunContext) -> list[Mail]:
    """Returns the newly imported mails (for security checks and classification)."""
    settings = ctx.settings
    snapshots: list[FolderSnapshot] = []
    for folder, role, category_id in _synced_folders(session, imap, account, categories):
        state = _state(session, account, folder, role, category_id)
        try:
            snapshots.append(snapshot_folder(imap, account, state, settings, ctx))
        except Exception as exc:  # noqa: BLE001
            ctx.error("fetch", f"folder {folder}: {exc}", account.name)
    session.flush()
    by_folder = {s.name: s for s in snapshots}
    sorted_category = {s.name: s.state.category_id for s in snapshots if s.role == "sorted"}

    # Known mail of this account that is supposed to sit in a synced folder.
    known: list[Mail] = list(session.scalars(select(Mail).where(
        Mail.account_id == account.id, Mail.folder.in_(list(by_folder)))))
    valid: dict[int, bool] = {}
    for mail in known:
        snap = by_folder[mail.folder]
        ok = (mail.uid is not None and mail.uidvalidity == snap.uidvalidity and mail.uid in snap.flags)
        valid[mail.id] = ok
        if ok:
            flags = snap.flags[mail.uid]
            mail.seen = "\\Seen" in flags
            mail.flagged = "\\Flagged" in flags

    imported: list[Mail] = []
    for snap in snapshots:
        to_import: list[FetchRecord] = []
        for rec in snap.new:
            if not rec.message_id:
                to_import.append(rec)  # identified after the full fetch (synthetic ID)
                continue
            mail = session.scalar(select(Mail).where(Mail.account_id == account.id,
                                                     Mail.message_id == rec.message_id))
            if mail is not None:
                if mail.folder == snap.name and mail.uid == rec.uid and mail.uidvalidity == snap.uidvalidity:
                    continue
                if valid.get(mail.id):
                    # Still at its known place (or already re-located this run): a second copy or a
                    # Gmail label. The first location wins.
                    continue
                previous_role = mail.folder_role
                _locate(mail, snap, rec)
                valid[mail.id] = True
                ctx.count("relocated")
                target_cat = sorted_category.get(snap.name)
                if snap.role == "sorted" and target_cat and mail.category_id != target_cat \
                        and previous_role in ("inbox", "sorted", "missing"):
                    record_correction(session, mail, target_cat, "mail_app", settings)
                    ctx.count("mail_app_corrections")
                continue
            tomb = session.scalar(select(Tombstone).where(Tombstone.account_id == account.id,
                                                          Tombstone.message_id == rec.message_id))
            if tomb is not None:
                tomb.folder, tomb.folder_role, tomb.uid, tomb.uidvalidity = (
                    snap.name, snap.role, rec.uid, snap.uidvalidity)
                tomb.flagged = rec.flagged
                continue
            to_import.append(rec)
        if to_import:
            imported.extend(_import(session, imap, account, snap, to_import, scanner, ctx))

    now = datetime.now(UTC)
    for mail in known:
        if not valid.get(mail.id):
            mail.folder_role = "missing"
            mail.folder = None
            mail.uid = None
            mail.missing_since = mail.missing_since or now
            ctx.count("missing")
    session.flush()
    return imported


def _import(session: Session, imap: ImapSession, account: Account, snap: FolderSnapshot,
            records: list[FetchRecord], scanner: Scanner, ctx: RunContext) -> list[Mail]:
    imported: list[Mail] = []
    for full in imap.fetch_full(snap.name, [r.uid for r in records]):
        if full.raw is None:
            continue
        try:
            parsed = parse_message(full.raw, full.internal_date)
        except Exception as exc:  # noqa: BLE001
            ctx.error("parse", f"{snap.name} uid {full.uid}: {exc}", account.name, alert=False)
            continue
        exists = session.scalar(select(Mail.id).where(Mail.account_id == account.id,
                                                      Mail.message_id == parsed.message_id))
        tomb = session.scalar(select(Tombstone.id).where(Tombstone.account_id == account.id,
                                                         Tombstone.message_id == parsed.message_id))
        if exists or tomb:
            continue
        received = full.internal_date or parsed.date or datetime.now(UTC)
        mail = Mail(
            account_id=account.id, message_id=parsed.message_id, thread_id=parsed.thread_id,
            in_reply_to=parsed.in_reply_to, references_header=" ".join(parsed.references) or None,
            folder_role=snap.role, folder=snap.name, uid=full.uid, uidvalidity=snap.uidvalidity,
            from_name=_clean(parsed.from_name), from_address=_clean(parsed.from_address),
            reply_to=_clean(parsed.reply_to) or None, to_addresses=_clean(parsed.to_addresses),
            cc_addresses=_clean(parsed.cc_addresses), subject=_clean(parsed.subject),
            received_at=received, size=full.size or len(full.raw), body_text=_clean(parsed.body_text),
            body_html=_clean(parsed.body_html) or None,
            headers={k: ([_clean(x) for x in v] if isinstance(v, list) else _clean(str(v)))
                     for k, v in parsed.headers.items()},
            seen=full.seen, flagged=full.flagged,
        )
        if snap.role == "sorted" and snap.state.category_id:
            # Mail the user put into a category folder themselves: that is its category.
            mail.category_id = snap.state.category_id
            mail.decided_by = "correction"
            mail.decision_detail = "filed into this category folder in a mail app"
            mail.confidence = 1.0
            mail.category_locked = True
        session.add(mail)
        session.flush()
        if snap.role != "sent":
            for att in parsed.attachments:
                stored_name, digest = attachment_store.store_bytes(att.payload)
                result = scanner.scan(att.payload)
                infected = result.status == "infected"
                session.add(Attachment(
                    mail_id=mail.id, filename=_clean(att.filename)[:500], stored_name=stored_name,
                    content_type=att.content_type[:255], size=len(att.payload), sha256=digest,
                    scan_status=result.status, scan_detail=result.detail,
                    risky=attachment_store.is_risky(att.filename, att.content_type, att.payload),
                    locked=infected,
                ))
                ctx.count(f"attachments_{result.status}")
        for link in parsed.links:
            session.add(Link(mail_id=mail.id, shown_text=_clean(link.shown_text), url=_clean(link.url)))
        imported.append(mail)
        ctx.count(f"fetched_{snap.role}")
    session.flush()
    return imported
