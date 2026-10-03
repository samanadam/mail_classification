"""Provider acceptance test: the plan's "Testing" checklist, run against a throwaway mailbox.

    mailcat provider-test <account> --yes-this-is-a-throwaway-account

It appends synthetic mail to the account, runs the real pipeline (with a deterministic stub
classifier) several times and checks every item of the checklist against the server state.
The same scenario runs in CI against a GreenMail server (tests/test_acceptance_imap.py).
"""
from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mailcat.classify.backends import ModelInput, ModelOutput
from mailcat.imap.client import ImapSession
from mailcat.models import Account, ActionLog, Category, Correction, FolderState, Mail, Run, Tombstone
from mailcat.worker.accounts import open_session
from mailcat.worker.run import execute_run
from mailcat.worker.undo import undo_run

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"\[cat:([^\]]+)\]")
TEST_PARENT = "MailcatTest"
HANDFILED = "MailcatTest-Handfiled"


class TagClassifier:
    """Deterministic stand-in for the model: "[cat:Finance]" in the subject means Finance."""

    version = "acceptance-stub"

    def classify(self, inputs: list[ModelInput], choices: dict[str, str]) -> list[ModelOutput]:
        outputs = []
        for item in inputs:
            match = _TAG_RE.search(item.subject)
            name = match.group(1) if match else None
            if name in choices:
                rest = (1 - 0.97) / max(len(choices) - 1, 1)
                outputs.append(ModelOutput({n: (0.97 if n == name else rest) for n in choices},
                                           phishing=0.0, needs_action=0.0))
            else:
                outputs.append(ModelOutput({n: 1 / len(choices) for n in choices}, phishing=0.0,
                                           needs_action=0.0))
        return outputs

    def close(self) -> None:
        pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(ok), detail))
        log.info("%s %s %s", "PASS" if ok else "FAIL", name, detail)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _message(tag: str, key: str, subject: str, date: datetime, extra_headers: dict[str, str] | None = None,
             body: str = "Acceptance test message.") -> tuple[str, bytes]:
    msg = EmailMessage()
    message_id = f"<{tag}-{key}@mailcat.test>"
    msg["Message-ID"] = message_id
    msg["From"] = "Acceptance Test <sender@example.org>"
    msg["To"] = "inbox@example.org"
    msg["Subject"] = f"{subject} ({tag}-{key})"
    msg["Date"] = format_datetime(date)
    for name, value in (extra_headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    return message_id, msg.as_bytes()


def _flags_of(imap: ImapSession, folder: str, message_id: str) -> tuple[str, ...] | None:
    uids = imap.search_message_id(folder, message_id)
    if not uids:
        return None
    meta = imap.fetch_meta(folder, uids[:1])
    return meta[0].flags if meta else None


def _date_of(imap: ImapSession, folder: str, message_id: str) -> datetime | None:
    uids = imap.search_message_id(folder, message_id)
    meta = imap.fetch_meta(folder, uids[:1]) if uids else []
    return meta[0].internal_date if meta else None


def _present(imap: ImapSession, folder: str | None, message_id: str) -> bool:
    return bool(folder) and bool(imap.search_message_id(folder, message_id))  # type: ignore[arg-type]


def run_scenario(session: Session, account: Account,
                 connect: Callable[[], ImapSession] | None = None, require_empty_inbox: bool = True) -> Report:
    report = Report()
    connect = connect or (lambda: open_session(session, account))
    cats = {c.name: c for c in session.scalars(select(Category))}
    for needed in ("Finance", "Promotions", "Personal", "Undecided", "Spam & Malicious"):
        if needed not in cats:
            raise RuntimeError(f"category {needed} is missing; run `mailcat init` first")
    tag = secrets.token_hex(4)
    now = datetime.now(UTC)
    old5, old10 = now - timedelta(days=5), now - timedelta(days=10)

    saved = {k: getattr(account, k) for k in (
        "sync_enabled", "writeback_enabled", "writeback_dry_run", "deletion_enabled", "deletion_dry_run",
        "backfill_enabled", "sorted_parent", "timer_overrides")}
    fin, promo, pers, und = cats["Finance"], cats["Promotions"], cats["Personal"], cats["Undecided"]
    account.sync_enabled = True
    account.writeback_enabled, account.writeback_dry_run = True, False
    account.deletion_enabled, account.deletion_dry_run = True, False
    account.backfill_enabled = False
    account.sorted_parent = TEST_PARENT
    account.timer_overrides = {
        str(fin.id): {"move_days": 1, "mailbox_delete_days": None, "local_delete_days": None},
        str(promo.id): {"move_days": 1, "mailbox_delete_days": 3, "local_delete_days": None},
        str(pers.id): {"move_days": 1, "mailbox_delete_days": None, "local_delete_days": 2},
        str(und.id): {"move_days": 1, "mailbox_delete_days": 1, "local_delete_days": None},
    }
    session.commit()
    overrides = {"initial_sync_days": 60, "protect_flagged": True, "protect_undecided": True,
                 "daily_delete_cap": 200, "move_batch_limit": 500, "thread_inheritance": False}
    classifier = TagClassifier()

    def run(**extra) -> Run:
        run_id = execute_run(kind="cli", triggered_by="provider-test", classifier=classifier,
                             account_ids=[account.id], settings_override={**overrides, **extra},
                             refresh_blocklists=False, send_alerts=False)
        session.expire_all()
        return session.get(Run, run_id)  # type: ignore[return-value]

    msgs: dict[str, tuple[str, bytes, str, datetime]] = {}

    def add(key: str, subject: str, flags: str, date: datetime, folder: str = "INBOX", **kw) -> None:
        mid, raw = _message(tag, key, subject, date, **kw)
        msgs[key] = (mid, raw, flags, date)
        imap.append(folder, raw, flags, date)

    try:
        with connect() as imap:
            if require_empty_inbox and imap.uids("INBOX"):
                raise RuntimeError("INBOX is not empty; use a dedicated throwaway account")
            if imap.resolve_role("trash") is None:
                imap.ensure_folder("Trash")
                imap.folders(refresh=True)
            imap.ensure_folder(HANDFILED)
            add("A", "[cat:Finance] unread old", "", old5)
            add("B", "[cat:Finance] read starred old", "\\Seen \\Flagged", old5)
            add("C", "[cat:Finance] unread new", "", now - timedelta(minutes=5))
            add("D", "[cat:Promotions] expired promo", "", old10)
            add("E", "[cat:Promotions] starred promo", "\\Flagged", old10)
            add("F", "[cat:Finance] neighbour", "\\Seen", old10)
            add("G", "[cat:?] nobody knows", "", old10)
            add("L", "[cat:Personal] local expiry", "\\Seen", old10)
            add("M", "[cat:Finance] spoofed invoice", "", old5, extra_headers={
                "Authentication-Results": "mx.test; spf=fail smtp.mailfrom=example.org; dkim=fail header.d=x"})
            add("H", "[cat:Promotions] hand filed", "\\Seen", old10, folder=HANDFILED)
            handfiled_before = imap.uids(HANDFILED)
            imap.folders(refresh=True)
            trash = imap.resolve_role("trash")
            all_mail = imap.resolve_role("all") if imap.is_gmail else None

        run1 = run()
        report.add("run 1 finished", run1.status in ("ok", "partial"), f"{run1.status} {run1.errors}")
        with connect() as imap:
            sorted_fin = imap.category_folder(TEST_PARENT, "Finance")
            sorted_promo = imap.category_folder(TEST_PARENT, "Promotions")
            sorted_spam = imap.category_folder(TEST_PARENT, "Spam & Malicious")
            unread_ok = True
            for key in ("A", "C", "E", "G", "M"):
                mid = msgs[key][0]
                for folder in ("INBOX", sorted_fin, sorted_promo, sorted_spam,
                               imap.category_folder(TEST_PARENT, "Undecided")):
                    if imap.folder_exists(folder) and _present(imap, folder, mid):
                        if "\\Seen" in (_flags_of(imap, folder, mid) or ()):
                            unread_ok = False
            report.add("1. every unread mail is still unread", unread_ok)
            report.add("2a. mail inside its grace period is not moved", _present(imap, "INBOX", msgs["C"][0]))
            report.add("2b. mail past its grace period moved to the right folder",
                       _present(imap, sorted_fin, msgs["A"][0]) and not _present(imap, "INBOX", msgs["A"][0]))
            flags_b = set(_flags_of(imap, sorted_fin, msgs["B"][0]) or ())
            date_b = _date_of(imap, sorted_fin, msgs["B"][0])
            report.add("3. moved mail keeps read state, star and date",
                       {"\\Seen", "\\Flagged"} <= flags_b and date_b is not None
                       and abs((date_b - msgs["B"][3]).total_seconds()) < 2, f"flags={flags_b} date={date_b}")
            report.add("4. deleted mail is gone from its folder and present in Trash",
                       not _present(imap, sorted_promo, msgs["D"][0]) and _present(imap, trash, msgs["D"][0]))
            if all_mail:
                report.add("5. Gmail: deleted mail is no longer in All Mail",
                           not _present(imap, all_mail, msgs["D"][0]))
            report.add("6. mail next to a deleted mail is untouched",
                       _present(imap, sorted_promo, msgs["E"][0])
                       and set(_flags_of(imap, sorted_promo, msgs["E"][0]) or ()) >= {"\\Flagged"}
                       and _present(imap, sorted_fin, msgs["F"][0]))
            undecided_folder = imap.category_folder(TEST_PARENT, "Undecided")
            report.add("8. starred, flagged and Undecided mail is never deleted",
                       _present(imap, sorted_promo, msgs["E"][0]) and _present(imap, undecided_folder, msgs["G"][0]))
            report.add("12. existing folders and hand-filed mail are untouched",
                       imap.uids(HANDFILED) == handfiled_before and _present(imap, HANDFILED, msgs["H"][0]))
        tomb = session.scalar(select(Tombstone).where(Tombstone.account_id == account.id,
                                                      Tombstone.message_id == msgs["L"][0]))
        report.add("11a. local copy deleted on its timer, tombstone kept", tomb is not None and session.scalar(
            select(Mail.id).where(Mail.account_id == account.id, Mail.message_id == msgs["L"][0])) is None)
        mail_m = session.scalar(select(Mail).where(Mail.account_id == account.id, Mail.message_id == msgs["M"][0]))
        report.add("14. failed SPF/DKIM sets the malicious flag",
                   mail_m is not None and mail_m.malicious and mail_m.category_id == cats["Spam & Malicious"].id,
                   f"{mail_m.malicious_reasons if mail_m else None}")

        run2 = run()
        counts2 = run2.counts or {}
        report.add("7. running twice moves and deletes nothing extra",
                   not counts2.get("moved") and not counts2.get("deleted_mailbox")
                   and not counts2.get("deleted_local"), str(counts2))
        report.add("11b. a locally deleted mail is not downloaded again", session.scalar(
            select(Mail.id).where(Mail.account_id == account.id, Mail.message_id == msgs["L"][0])) is None)

        account = session.get(Account, account.id)  # type: ignore[assignment]
        account.timer_overrides = {**account.timer_overrides,
                                   str(fin.id): {"move_days": 0, "mailbox_delete_days": None,
                                                 "local_delete_days": None}}
        session.commit()
        run3 = run()
        with connect() as imap:
            moved_c = _present(imap, sorted_fin, msgs["C"][0]) and not _present(imap, "INBOX", msgs["C"][0])
        report.add("13. a changed setting takes effect at the next run without a restart", moved_c,
                   str(run3.counts))

        counts = undo_run(session, run3.id, triggered_by="provider-test")
        with connect() as imap:
            report.add("10. the undo command restores a run's moves",
                       _present(imap, "INBOX", msgs["C"][0]) and not _present(imap, sorted_fin, msgs["C"][0]),
                       str(counts))
            add("N1", "[cat:Promotions] cap one", "", old10)
            add("N2", "[cat:Promotions] cap two", "", old10)
        run4 = run(daily_delete_cap=1)
        run5_errors = [e for e in (run4.errors or []) if e.get("step") == "retention" and "cap" in e["message"]]
        with connect() as imap:
            still = all(_present(imap, sorted_promo, msgs[k][0]) for k in ("N1", "N2"))
        report.add("9. a run over the deletion cap stops and alerts",
                   bool(run5_errors) and run5_errors[0].get("alert") and still, str(run4.errors))
    finally:
        account = session.get(Account, account.id)  # type: ignore[assignment]
        for key, value in saved.items():
            setattr(account, key, value)
        ids = [m[0] for m in msgs.values()]
        mail_ids = list(session.scalars(select(Mail.id).where(Mail.account_id == account.id,
                                                              Mail.message_id.in_(ids))))
        session.execute(delete(Correction).where(Correction.mail_id.in_(mail_ids)))
        session.execute(delete(ActionLog).where(ActionLog.message_id.in_(ids)))
        for mail in session.scalars(select(Mail).where(Mail.id.in_(mail_ids))):
            session.delete(mail)
        session.execute(delete(Tombstone).where(Tombstone.account_id == account.id,
                                                Tombstone.message_id.in_(ids)))
        session.execute(delete(FolderState).where(FolderState.account_id == account.id,
                                                  FolderState.folder.like(f"{TEST_PARENT}%")))
        session.commit()
    return report
