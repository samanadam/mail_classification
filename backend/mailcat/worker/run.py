"""One daily run, in the order of the project plan:

1. Refresh blocklists (and ClamAV signatures).
2. Fetch new mail from every enabled account.
3. Scan attachments, check links and headers.
4. Classify with rules, then the model.
5. Move inbox mail older than its grace period into its category folder.
6. Apply retention.
7. Verify (done inline per action), write logs, alert on failures.

Every step is idempotent, so an interrupted run is completed by the next one.
"""
from __future__ import annotations

import gc
import logging
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.classify.backends import Classifier, build_classifier
from mailcat.classify.engine import CLASSIFIABLE_ROLES, CategorySet, classify_mails
from mailcat.db import get_engine, new_session
from mailcat.ingest.clamav import Scanner, update_signatures
from mailcat.models import Account, Category, Mail, Run
from mailcat.security import blocklists
from mailcat.worker.accounts import open_session
from mailcat.worker.alerts import send_alert
from mailcat.worker.context import RunContext
from mailcat.worker.policy import live_delete_count, plan_deletions
from mailcat.worker.retention import delete_from_mailbox, delete_local
from mailcat.worker.security_pass import assess_mail
from mailcat.worker.sync import sync_account
from mailcat.worker.writeback import run_writeback

log = logging.getLogger("mailcat.run")

RUN_LOCK_ID = 7_310_431
CLASSIFY_CHUNK = 256


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def run_lock() -> Iterator[None]:
    """Postgres advisory lock: only one run at a time, across containers."""
    conn = get_engine().connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": RUN_LOCK_ID}).scalar()
        if not got:
            raise AlreadyRunning("another run is in progress")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": RUN_LOCK_ID})
    finally:
        conn.close()


def _heartbeat(session: Session, run: Run) -> None:
    run.heartbeat_at = datetime.now(UTC)
    session.commit()


def step_refresh_blocklists(ctx: RunContext) -> blocklists.Blocklist:
    feeds = ctx.settings["blocklist_feeds"]
    try:
        status = blocklists.refresh(feeds, ctx.settings.get("phishtank_url") or "")
        for name, result in status.items():
            if result.startswith("error"):
                ctx.error("blocklists", f"{name}: {result}", alert=False)
    except Exception as exc:  # noqa: BLE001 - stale lists are better than no run
        ctx.error("blocklists", str(exc), alert=False)
    if not update_signatures():
        ctx.error("clamav", "signature update failed; using the previous signatures", alert=False)
    blocklist = blocklists.load(feeds)
    ctx.count("blocklist_entries", len(blocklist))
    return blocklist


def step_fetch(session: Session, ctx: RunContext, accounts: list[Account], categories: list[Category],
               run: Run) -> list[int]:
    new_ids: list[int] = []
    with Scanner() as scanner:
        for account in accounts:
            try:
                with open_session(session, account) as imap:
                    imported = sync_account(session, imap, account, categories, scanner, ctx)
                new_ids.extend(m.id for m in imported)
                account.last_sync_ok = True
                account.last_sync_error = None
                account.consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 - one broken account must not stop the others
                session.rollback()
                account = session.get(Account, account.id)  # type: ignore[assignment]
                account.last_sync_ok = False
                account.last_sync_error = str(exc)[:2000]
                account.consecutive_failures += 1
                alert = account.consecutive_failures >= int(ctx.settings["alert_after_failures"])
                ctx.error("fetch", f"sync failed ({account.consecutive_failures}x): {exc}", account.name,
                          alert=alert)
            account.last_sync_at = datetime.now(UTC)
            _heartbeat(session, run)
    return new_ids


def step_security(session: Session, ctx: RunContext, mail_ids: list[int],
                  blocklist: blocklists.Blocklist) -> None:
    for start in range(0, len(mail_ids), 200):
        for mail in session.scalars(select(Mail).where(Mail.id.in_(mail_ids[start:start + 200]))):
            if mail.folder_role == "sent":
                continue
            assess_mail(mail, blocklist)
            if mail.malicious:
                ctx.count("malicious")
        session.commit()


def step_classify(session: Session, ctx: RunContext, run: Run, classifier: Classifier | None = None) -> None:
    pending = select(Mail.id).where(Mail.classified_at.is_(None), Mail.folder_role.in_(CLASSIFIABLE_ROLES))
    if session.scalar(select(Mail.id).where(Mail.id.in_(pending)).limit(1)) is None:
        return
    owned = classifier is None
    try:
        classifier = classifier or build_classifier()
    except Exception as exc:  # noqa: BLE001
        ctx.error("classify", f"model could not be loaded: {exc}")
        return
    try:
        categories = CategorySet.load(session)
        names = {a.id: a.name for a in session.scalars(select(Account))}
        last_id = 0
        while True:
            batch = list(session.scalars(
                select(Mail).where(Mail.classified_at.is_(None), Mail.folder_role.in_(CLASSIFIABLE_ROLES),
                                   Mail.id > last_id).order_by(Mail.id).limit(CLASSIFY_CHUNK)))
            if not batch:
                break
            last_id = batch[-1].id
            counts = classify_mails(session, batch, classifier, ctx.settings, categories, names)
            for key, value in counts.items():
                ctx.count(f"classified_{key}", value)
            _heartbeat(session, run)
    finally:
        if owned and classifier is not None:
            classifier.close()
            del classifier
            gc.collect()


def step_writeback(session: Session, ctx: RunContext, accounts: list[Account]) -> None:
    categories = {c.id: c for c in session.scalars(select(Category))}
    for account in accounts:
        if not account.writeback_enabled or account.last_sync_ok is False:
            continue
        try:
            with open_session(session, account) as imap:
                run_writeback(session, imap, account, categories, ctx)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.commit()  # keep the log lines of moves that did happen
            ctx.error("writeback", str(exc), account.name)


def step_retention(session: Session, ctx: RunContext, accounts: list[Account]) -> None:
    now = datetime.now(UTC)
    ids = [a.id for a in accounts if a.deletion_enabled]
    if not ids:
        return
    plans = plan_deletions(session, ctx.settings, now, account_ids=ids)
    cap = int(ctx.settings["daily_delete_cap"])
    wanted = live_delete_count(plans)
    ctx.count("deletes_wanted", wanted)
    if wanted > cap:
        ctx.error("retention", f"run wants to delete {wanted} mails, over the daily cap of {cap}; "
                               "nothing was deleted. Check the deletion preview, then raise the cap or "
                               "fix the timers.")
        return
    by_account: dict[int, list] = defaultdict(list)
    for plan in plans:
        by_account[plan.account.id].append(plan)
    for account in accounts:
        account_plans = by_account.get(account.id, [])
        mailbox = [p for p in account_plans if p.kind == "mailbox"]
        local = [p for p in account_plans if p.kind == "local"]
        failed: set[str] = set()
        if mailbox:
            if account.last_sync_ok is False:
                failed = {p.message_id for p in mailbox}
            else:
                try:
                    with open_session(session, account) as imap:
                        failed = delete_from_mailbox(session, imap, account, mailbox, ctx, now)
                    session.commit()
                except Exception as exc:  # noqa: BLE001
                    session.commit()
                    ctx.error("retention", str(exc), account.name)
                    failed = {p.message_id for p in mailbox}
        try:
            delete_local(session, local, ctx, skip=failed)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            ctx.error("retention", f"local delete failed: {exc}", account.name)


def execute_run(kind: str = "scheduled", triggered_by: str | None = None,
                classifier: Classifier | None = None, run_id: int | None = None,
                account_ids: list[int] | None = None, settings_override: dict | None = None,
                refresh_blocklists: bool = True, send_alerts: bool = True) -> int:
    """Run the whole pipeline once. `account_ids`, `settings_override` and the switches exist for
    the provider acceptance test; the scheduler always runs everything."""
    with run_lock():
        session = new_session()
        try:
            run = session.get(Run, run_id) if run_id else None
            if run is None:
                run = Run(kind=kind, triggered_by=triggered_by, status="running")
                session.add(run)
            run.status = "running"
            run.started_at = datetime.now(UTC)
            session.commit()
            settings = {**settings_store.get_all(session), **(settings_override or {})}
            ctx = RunContext(run_id=run.id, settings=settings)
            log.info("run %s started (%s)", run.id, kind)
            try:
                if refresh_blocklists:
                    blocklist = step_refresh_blocklists(ctx)
                else:
                    blocklist = blocklists.load(ctx.settings["blocklist_feeds"])
                _heartbeat(session, run)
                categories = list(session.scalars(select(Category).order_by(Category.position)))
                query = select(Account).where(Account.sync_enabled.is_(True)).order_by(Account.id)
                if account_ids is not None:
                    query = query.where(Account.id.in_(account_ids))
                else:
                    # The provider test's sandbox account only runs when the test asks for it.
                    query = query.where(Account.sandbox_root.is_(None))
                accounts = list(session.scalars(query))
                new_ids = step_fetch(session, ctx, accounts, categories, run)
                step_security(session, ctx, new_ids, blocklist)
                step_classify(session, ctx, run, classifier)
                step_writeback(session, ctx, accounts)
                step_retention(session, ctx, accounts)
                status = "partial" if ctx.errors else "ok"
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                log.exception("run %s failed", run.id)
                ctx.error("run", f"run failed: {exc}")
                status = "failed"
            run = session.get(Run, run.id)  # type: ignore[assignment]
            run.status = status
            run.finished_at = datetime.now(UTC)
            run.counts = dict(ctx.counts)
            run.errors = ctx.errors[:500]
            session.commit()
            log.info("run %s finished: %s %s", run.id, status, dict(ctx.counts))
            if send_alerts and ctx.alerts:
                send_alert(f"Mail categorizer run #{run.id}: {status}", ctx.alerts[:20])
            elif send_alerts and ctx.settings.get("alert_on_success"):
                summary = ", ".join(f"{k}={v}" for k, v in sorted(ctx.counts.items()))
                send_alert(f"Mail categorizer run #{run.id}: ok", [summary or "nothing to do"])
            return run.id  # type: ignore[return-value]
        finally:
            session.close()
