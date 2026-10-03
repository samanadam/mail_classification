"""The plan's Testing checklist, end to end against a real IMAP server (GreenMail)."""
import secrets

import pytest

from mailcat.imap.client import ImapSession
from mailcat.ops.acceptance import run_scenario
from tests import factories as f

pytestmark = pytest.mark.imap


def _account(db, host, port):
    user = f"t{secrets.token_hex(4)}@localhost"
    acc = f.account(db, host=host, port=port, security="plain", username=user, email=user)
    db.commit()
    return acc


def _real_mailbox(host, port, acc) -> ImapSession:
    """A mailbox full of "real" mail in INBOX, Sent, a user folder and an existing Sorted folder."""
    from datetime import UTC, datetime, timedelta

    from mailcat.ops.acceptance import _message

    imap = ImapSession(host, port, "custom", security="plain")
    imap.login_password(acc.username, "x")
    for folder in ("Trash", "Sent", "Archive", "Sorted.Finance"):
        imap.ensure_folder(folder)
    old = datetime.now(UTC) - timedelta(days=30)
    for i, (folder, flags) in enumerate([
        ("INBOX", ""), ("INBOX", "\\Seen"), ("INBOX", "\\Flagged"), ("INBOX", "\\Seen \\Deleted"),
        ("Sent", "\\Seen"), ("Archive", ""), ("Sorted.Finance", "\\Seen"),
    ]):
        # "[cat:Promotions]" would be deleted on the test's 3-day timer if the test ever saw this mail.
        _, raw = _message("real", str(i), f"[cat:Promotions] real mail {i}", old)
        imap.append(folder, raw.replace(b"@mailcat.test>", b"@real.example>"), flags, old)
    return imap


def _state(imap: ImapSession) -> dict:
    return {f.name: imap.fetch_flags(f.name) if imap.uids(f.name) else {}
            for f in imap.folders(refresh=True) if f.name != "Trash"}


def test_provider_checklist_never_touches_real_mail(db, imap_server):
    from sqlalchemy import select

    from mailcat.models import Account, Mail

    host, port = imap_server
    acc = _account(db, host, port)
    acc.timer_overrides = {"x": 1}
    db.commit()
    imap = _real_mailbox(host, port, acc)
    before = _state(imap)
    report = run_scenario(db, acc)
    failed = [f"{c.name}: {c.detail}" for c in report.checks if not c.ok]
    assert not failed, "\n".join(failed)
    assert len(report.checks) >= 17
    after = _state(imap)
    # Same real folders, UIDs and flags. GreenMail crashes on DELETE of a folder other sessions
    # once selected, so the (emptied) sandbox folders may remain here; real servers remove them.
    sandbox = {name: flags for name, flags in after.items() if name.startswith("MailcatTest")}
    assert {name: flags for name, flags in after.items() if name not in sandbox} == before
    assert all(not flags for flags in sandbox.values())  # every test mail went to Trash
    assert not imap.search_message_id("Trash", "<real-0@real.example>")
    db.expire_all()
    real = db.get(Account, acc.id)
    assert real.timer_overrides == {"x": 1} and real.writeback_enabled is False and real.sandbox_root is None
    assert db.scalar(select(Account).where(Account.sandbox_root.is_not(None))) is None
    assert db.scalar(select(Mail.id).where(Mail.account_id == acc.id)) is None
    imap.close()


def test_write_guards(db, imap_server):
    from mailcat.imap.client import WriteRefused

    host, port = imap_server
    acc = _account(db, host, port)
    setup = _real_mailbox(host, port, acc)
    uid = setup.uids("INBOX")[0]
    setup.ensure_folder("MailcatTest.Inbox")
    setup.close()

    locked = ImapSession(host, port, "custom", security="plain", read_only=True)
    locked.login_password(acc.username, "x")
    for attempt in (lambda: locked.move("INBOX", uid, "Archive"), lambda: locked.ensure_folder("New"),
                    lambda: locked.append("INBOX", b"Subject: x\r\n\r\nx"), lambda: locked.select("INBOX", False),
                    lambda: locked.set_flag("INBOX", uid, "\\Seen", True), lambda: locked.delete_folder("Archive"),
                    lambda: locked.rename_folder("Archive", "Archive2")):
        with pytest.raises(WriteRefused):
            attempt()
    assert locked.uids("INBOX")  # reads still work
    locked.close()

    box = ImapSession(host, port, "custom", security="plain", write_scope="MailcatTest")
    box.login_password(acc.username, "x")
    for attempt in (lambda: box.move("INBOX", uid, "MailcatTest.Inbox"), lambda: box.move("INBOX", uid, "Trash"),
                    lambda: box.ensure_folder("Elsewhere"), lambda: box.append("INBOX", b"Subject: x\r\n\r\nx"),
                    lambda: box.select("INBOX", False), lambda: box.delete_folder("Archive"),
                    lambda: box.ensure_folder("MailcatTestX")):
        with pytest.raises(WriteRefused):
            attempt()
    box.append("MailcatTest.Inbox", b"Message-ID: <t@mailcat.test>\r\nSubject: x\r\n\r\nx")
    [own] = box.uids("MailcatTest.Inbox")
    box.move("MailcatTest.Inbox", own, "Trash")  # its own test mail may go to Trash
    assert box.search_message_id("Trash", "<t@mailcat.test>")
    assert len(box.uids("INBOX")) == 4
    box.close()


def test_read_only_switch_turns_live_runs_into_dry_runs(db, imap_server, monkeypatch):
    from sqlalchemy import select

    from mailcat.config import get_config
    from mailcat.models import ActionLog
    from mailcat.ops.acceptance import TagClassifier
    from mailcat.worker.run import execute_run

    host, port = imap_server
    acc = _account(db, host, port)
    acc.writeback_enabled, acc.writeback_dry_run = True, False
    acc.deletion_enabled, acc.deletion_dry_run = True, False
    db.commit()
    imap = _real_mailbox(host, port, acc)
    before = _state(imap)
    monkeypatch.setattr(get_config(), "read_only", True)
    execute_run(kind="cli", classifier=TagClassifier(), account_ids=[acc.id], refresh_blocklists=False,
                send_alerts=False, settings_override={"initial_sync_days": 60})
    assert _state(imap) == before
    logged = list(db.scalars(select(ActionLog).where(ActionLog.account_id == acc.id,
                                                     ActionLog.action.in_(("move", "delete")))))
    assert logged and all(entry.dry_run for entry in logged)
    imap.close()


def test_reads_never_change_flags(db, imap_server):
    host, port = imap_server
    acc = _account(db, host, port)
    imap = ImapSession(host, port, "custom", security="plain")
    imap.login_password(acc.username, "x")
    imap.append("INBOX", b"Message-ID: <p@x>\r\nSubject: s\r\n\r\nbody", "", None)
    [uid] = imap.uids("INBOX")
    list(imap.fetch_full("INBOX", [uid]))
    imap.fetch_meta("INBOX", [uid])
    assert "\\Seen" not in imap.fetch_flags("INBOX")[uid]
    imap.close()


def test_mail_app_drag_is_a_correction_and_hand_moves_are_left_alone(db, imap_server):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from mailcat.models import Correction, Mail
    from mailcat.ops.acceptance import TagClassifier, _message
    from mailcat.worker.run import execute_run

    host, port = imap_server
    acc = _account(db, host, port)
    acc.writeback_enabled, acc.writeback_dry_run = True, False
    db.commit()
    old = datetime.now(UTC) - timedelta(days=3)
    imap = ImapSession(host, port, "custom", security="plain")
    imap.login_password(acc.username, "x")
    drag_id, raw = _message("drag", "1", "[cat:Finance] receipt", old)
    imap.append("INBOX", raw, "", old)
    hand_id, raw = _message("hand", "1", "[cat:Finance] keep me", old)
    imap.append("INBOX", raw, "", old)
    imap.ensure_folder("Archive")

    def run():
        execute_run(kind="cli", classifier=TagClassifier(), account_ids=[acc.id], refresh_blocklists=False,
                    send_alerts=False, settings_override={"initial_sync_days": 30})
        db.expire_all()

    run()
    finance = imap.category_folder("Sorted", "Finance")
    personal = imap.category_folder("Sorted", "Personal")
    assert imap.search_message_id(finance, drag_id)
    # the user drags one mail to another category folder and files the other one by hand
    imap.ensure_folder(personal)
    imap.move(finance, imap.search_message_id(finance, drag_id)[0], personal)
    imap.move(finance, imap.search_message_id(finance, hand_id)[0], "Archive")
    run()
    dragged = db.scalar(select(Mail).where(Mail.message_id == drag_id))
    assert dragged.category_id == f.category(db, "Personal").id and dragged.category_locked
    corr = db.scalar(select(Correction).where(Correction.mail_id == dragged.id))
    assert corr.source == "mail_app" and corr.old_category_id == f.category(db, "Finance").id
    assert corr.features["subject"].startswith("[cat:Finance]")
    hand = db.scalar(select(Mail).where(Mail.message_id == hand_id))
    assert hand.folder_role == "missing" and hand.folder is None
    run()  # nothing moves back
    assert imap.search_message_id(personal, drag_id) and imap.search_message_id("Archive", hand_id)
    imap.close()
