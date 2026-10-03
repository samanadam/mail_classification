"""Provider acceptance test: the plan's "Testing" checklist, run safely inside a real mailbox.

    mailcat provider-test <account> --yes

The test never touches existing mail. It runs on a temporary sandbox copy of the account whose
IMAP session can only write inside the MailcatTest folder tree (plus moving its own test mail to
Trash); the IMAP client enforces this. It appends synthetic mail to MailcatTest/Inbox, runs the
real pipeline (with a deterministic stub classifier) several times and checks every item of the
checklist against the server. Every other folder is fingerprinted (UIDVALIDITY, UIDs and flags)
before and after, and the test fails if any existing mail moved, vanished or changed flags.
Afterwards the sandbox folders are removed (only if they hold nothing but test mail); deleted
test mail stays in Trash until the provider empties it.

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
from mailcat.models import Account, ActionLog, Category, Mail, Run, Tombstone
from mailcat.worker.accounts import inbox_folder, open_session
from mailcat.worker.run import execute_run
from mailcat.worker.undo import undo_run

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"\[cat:([^\]]+)\]")
TEST_PARENT = "MailcatTest"
TEST_DOMAIN = "@mailcat.test>"
# Server-maintained views that legitimately show test mail (Gmail All Mail, Starred, Important).
_VIRTUAL_FLAGS = {"\\all", "\\flagged", "\\important", "\\trash", "\\noselect", "\\nonexistent"}


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


def _fingerprint(imap: ImapSession) -> dict[str, tuple[int, dict[int, frozenset[str]]]]:
    """UIDVALIDITY and UID -> flags of every real folder (outside the sandbox, Trash and server views)."""
    trash = imap.resolve_role("trash")
    result = {}
    for folder in imap.folders(refresh=True):
        if folder.name == trash or imap.in_scope(folder.name):
            continue
        if any(flag.lower() in _VIRTUAL_FLAGS for flag in folder.flags):
            continue
        uidvalidity, count = imap.select(folder.name, readonly=True)
        flags = imap.fetch_flags(folder.name) if count else {}
        result[folder.name] = (uidvalidity, {uid: frozenset(f) - {"\\Recent"} for uid, f in flags.items()})
    return result


def _compare(before: dict, after: dict) -> list[str]:
    """Every message that existed before must still be in its folder with the same flags.
    New mail arriving during the test is fine."""
    problems = []
    for folder, (uidvalidity, flags) in before.items():
        if folder not in after:
            problems.append(f"{folder}: folder disappeared")
            continue
        uidvalidity2, flags2 = after[folder]
        if uidvalidity2 != uidvalidity:
            problems.append(f"{folder}: UIDVALIDITY changed")
            continue
        for uid, value in flags.items():
            if uid not in flags2:
                problems.append(f"{folder}: message UID {uid} is gone")
            elif flags2[uid] != value:
                problems.append(f"{folder}: UID {uid} flags {sorted(value)} -> {sorted(flags2[uid])}")
    return problems


def _sandbox_folders(imap: ImapSession) -> tuple[list[str], list[str]]:
    """The sandbox folders (deepest first) and any message in them that is not test mail."""
    folders = sorted((f.name for f in imap.folders(refresh=True) if imap.in_scope(f.name)),
                     key=lambda n: n.count(imap.delimiter), reverse=True)
    foreign = []
    for name in folders:
        uids = imap.uids(name)
        for rec in imap.fetch_meta(name, uids) if uids else []:
            if not (rec.message_id or "").endswith(TEST_DOMAIN):
                foreign.append(f"{name}: {rec.message_id}")
    return folders, foreign


def _remove_sandbox(connect: Callable[[], ImapSession]) -> str:
    """Move the test mail to Trash (on Gmail, deleting a label would leave it in All Mail), then
    delete the sandbox folders on a fresh connection that has none of them selected."""
    with connect() as imap:
        folders, foreign = _sandbox_folders(imap)
        if foreign:
            return f"left {TEST_PARENT} in place: it holds mail that is not test mail ({foreign[:3]})"
        trash = imap.resolve_role("trash")
        for name in folders:
            for uid in reversed(imap.uids(name)) if trash else []:
                imap.move(name, uid, trash)  # type: ignore[arg-type]
    with connect() as imap:
        for name in folders:
            imap.delete_folder(name)
    return f"removed {len(folders)} sandbox folder(s)"


def _drop_sandbox_accounts(session: Session) -> None:
    for old in session.scalars(select(Account).where(Account.sandbox_root.is_not(None))):
        session.execute(delete(ActionLog).where(ActionLog.account_id == old.id))
        session.delete(old)
    session.commit()


def run_scenario(session: Session, account: Account,
                 connect: Callable[[Account], ImapSession] | None = None) -> Report:
    report = Report()
    connect = connect or (lambda acc: open_session(session, acc))
    cats = {c.name: c for c in session.scalars(select(Category))}
    for needed in ("Finance", "Promotions", "Personal", "Undecided", "Spam & Malicious"):
        if needed not in cats:
            raise RuntimeError(f"category {needed} is missing; run `mailcat init` first")
    if account.sandbox_root:
        raise RuntimeError("pick the real account, not a sandbox account")
    tag = secrets.token_hex(4)
    now = datetime.now(UTC)
    old5, old10 = now - timedelta(days=5), now - timedelta(days=10)
    fin, promo, pers, und = cats["Finance"], cats["Promotions"], cats["Personal"], cats["Undecided"]

    # A temporary copy of the account. The real account row is never modified.
    _drop_sandbox_accounts(session)
    box = Account(
        name=f"{account.name[:80]} [provider-test]", provider=account.provider, email=account.email,
        host=account.host, port=account.port, security=account.security, username=account.username,
        auth_method=account.auth_method, credentials_enc=account.credentials_enc,
        folder_overrides=dict(account.folder_overrides or {}), sandbox_root=TEST_PARENT,
        sorted_parent=TEST_PARENT, sync_enabled=True, writeback_enabled=True, writeback_dry_run=False,
        deletion_enabled=True, deletion_dry_run=False, backfill_enabled=False, timer_overrides={
            str(fin.id): {"move_days": 1, "mailbox_delete_days": None, "local_delete_days": None},
            str(promo.id): {"move_days": 1, "mailbox_delete_days": 3, "local_delete_days": None},
            str(pers.id): {"move_days": 1, "mailbox_delete_days": None, "local_delete_days": 2},
            str(und.id): {"move_days": 1, "mailbox_delete_days": 1, "local_delete_days": None},
        })
    session.add(box)
    session.commit()
    box_id = box.id
    overrides = {"initial_sync_days": 60, "protect_flagged": True, "protect_undecided": True,
                 "daily_delete_cap": 200, "move_batch_limit": 500, "thread_inheritance": False}
    classifier = TagClassifier()

    def run(**extra) -> Run:
        run_id = execute_run(kind="cli", triggered_by="provider-test", classifier=classifier,
                             account_ids=[box_id], settings_override={**overrides, **extra},
                             refresh_blocklists=False, send_alerts=False)
        session.expire_all()
        result = session.get(Run, run_id) if run_id else None
        if result is None:
            raise RuntimeError("another run is in progress; pause runs in Settings and try again")
        return result

    msgs: dict[str, tuple[str, bytes, str, datetime]] = {}
    inbox = handfiled = ""

    def add(imap: ImapSession, key: str, subject: str, flags: str, date: datetime, folder: str | None = None,
            **kw) -> None:
        mid, raw = _message(tag, key, subject, date, **kw)
        msgs[key] = (mid, raw, flags, date)
        imap.append(folder or inbox, raw, flags, date)

    try:
        with connect(box) as imap:
            if imap.write_scope != TEST_PARENT:
                raise RuntimeError("the sandbox session is not confined to the test folders; refusing to run")
            leftover = any(imap.in_scope(f.name) for f in imap.folders(refresh=True))
        if leftover:  # from an interrupted test
            note = _remove_sandbox(lambda: connect(box))
            if not note.startswith("removed"):
                raise RuntimeError(note)
        with connect(box) as imap:
            trash = imap.resolve_role("trash")
            if trash is None:
                raise RuntimeError("no Trash folder found; set a trash folder override for this account")
            before = _fingerprint(imap)
            inbox = inbox_folder(imap, box)
            handfiled = imap.category_folder(TEST_PARENT, "Handfiled")
            imap.ensure_folder(inbox)
            imap.ensure_folder(handfiled)
            add(imap, "A", "[cat:Finance] unread old", "", old5)
            add(imap, "B", "[cat:Finance] read starred old", "\\Seen \\Flagged", old5)
            add(imap, "C", "[cat:Finance] unread new", "", now - timedelta(minutes=5))
            add(imap, "D", "[cat:Promotions] expired promo", "", old10)
            add(imap, "E", "[cat:Promotions] starred promo", "\\Flagged", old10)
            add(imap, "F", "[cat:Finance] neighbour", "\\Seen", old10)
            add(imap, "G", "[cat:?] nobody knows", "", old10)
            add(imap, "L", "[cat:Personal] local expiry", "\\Seen", old10)
            add(imap, "M", "[cat:Finance] spoofed invoice", "", old5, extra_headers={
                "Authentication-Results": "mx.test; spf=fail smtp.mailfrom=example.org; dkim=fail header.d=x"})
            add(imap, "H", "[cat:Promotions] hand filed", "\\Seen", old10, folder=handfiled)
            handfiled_before = imap.uids(handfiled)
            imap.folders(refresh=True)
            all_mail = imap.resolve_role("all") if imap.is_gmail else None

        run1 = run()
        report.add("run 1 finished", run1.status in ("ok", "partial"), f"{run1.status} {run1.errors}")
        with connect(box) as imap:
            sorted_fin = imap.category_folder(TEST_PARENT, "Finance")
            sorted_promo = imap.category_folder(TEST_PARENT, "Promotions")
            sorted_spam = imap.category_folder(TEST_PARENT, "Spam & Malicious")
            undecided = imap.category_folder(TEST_PARENT, "Undecided")
            unread_ok = True
            for key in ("A", "C", "E", "G", "M"):
                mid = msgs[key][0]
                for folder in (inbox, sorted_fin, sorted_promo, sorted_spam, undecided):
                    if imap.folder_exists(folder) and _present(imap, folder, mid):
                        if "\\Seen" in (_flags_of(imap, folder, mid) or ()):
                            unread_ok = False
            report.add("1. every unread mail is still unread", unread_ok)
            report.add("2a. mail inside its grace period is not moved", _present(imap, inbox, msgs["C"][0]))
            report.add("2b. mail past its grace period moved to the right folder",
                       _present(imap, sorted_fin, msgs["A"][0]) and not _present(imap, inbox, msgs["A"][0]))
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
            report.add("8. starred, flagged and Undecided mail is never deleted",
                       _present(imap, sorted_promo, msgs["E"][0]) and _present(imap, undecided, msgs["G"][0]))
            report.add("12. existing folders and hand-filed mail are untouched",
                       imap.uids(handfiled) == handfiled_before and _present(imap, handfiled, msgs["H"][0]))
        tomb = session.scalar(select(Tombstone).where(Tombstone.account_id == box_id,
                                                      Tombstone.message_id == msgs["L"][0]))
        report.add("11a. local copy deleted on its timer, tombstone kept", tomb is not None and session.scalar(
            select(Mail.id).where(Mail.account_id == box_id, Mail.message_id == msgs["L"][0])) is None)
        mail_m = session.scalar(select(Mail).where(Mail.account_id == box_id, Mail.message_id == msgs["M"][0]))
        report.add("14. failed SPF/DKIM sets the malicious flag",
                   mail_m is not None and mail_m.malicious and mail_m.category_id == cats["Spam & Malicious"].id,
                   f"{mail_m.malicious_reasons if mail_m else None}")

        run2 = run()
        counts2 = run2.counts or {}
        report.add("7. running twice moves and deletes nothing extra",
                   not counts2.get("moved") and not counts2.get("deleted_mailbox")
                   and not counts2.get("deleted_local"), str(counts2))
        report.add("11b. a locally deleted mail is not downloaded again", session.scalar(
            select(Mail.id).where(Mail.account_id == box_id, Mail.message_id == msgs["L"][0])) is None)

        box = session.get(Account, box_id)  # type: ignore[assignment]
        box.timer_overrides = {**box.timer_overrides,
                               str(fin.id): {"move_days": 0, "mailbox_delete_days": None, "local_delete_days": None}}
        session.commit()
        run3 = run()
        with connect(box) as imap:
            moved_c = _present(imap, sorted_fin, msgs["C"][0]) and not _present(imap, inbox, msgs["C"][0])
        report.add("13. a changed setting takes effect at the next run without a restart", moved_c,
                   str(run3.counts))

        counts = undo_run(session, run3.id, triggered_by="provider-test")
        with connect(box) as imap:
            report.add("10. the undo command restores a run's moves",
                       _present(imap, inbox, msgs["C"][0]) and not _present(imap, sorted_fin, msgs["C"][0]),
                       str(counts))
            add(imap, "N1", "[cat:Promotions] cap one", "", old10)
            add(imap, "N2", "[cat:Promotions] cap two", "", old10)
        run4 = run(daily_delete_cap=1)
        cap_errors = [e for e in (run4.errors or []) if e.get("step") == "retention" and "cap" in e["message"]]
        with connect(box) as imap:
            still = all(_present(imap, sorted_promo, msgs[k][0]) for k in ("N1", "N2"))
        report.add("9. a run over the deletion cap stops and alerts",
                   bool(cap_errors) and cap_errors[0].get("alert") and still, str(run4.errors))
        with connect(box) as imap:
            problems = _compare(before, _fingerprint(imap))
        report.add("15. existing mail outside the sandbox is unchanged (every folder: UIDs and flags)",
                   not problems, f"{len(before)} folders checked; " + "; ".join(problems[:10]))
    finally:
        box = session.get(Account, box_id)  # type: ignore[assignment]
        if box is not None:
            try:
                log.info("cleanup: %s", _remove_sandbox(lambda: connect(box)))
            except Exception as exc:  # noqa: BLE001
                log.warning("cleanup: could not remove the %s folders: %s", TEST_PARENT, exc)
        _drop_sandbox_accounts(session)
    return report
