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


def test_provider_checklist(db, imap_server):
    host, port = imap_server
    acc = _account(db, host, port)
    report = run_scenario(db, acc)
    failed = [f"{c.name}: {c.detail}" for c in report.checks if not c.ok]
    assert not failed, "\n".join(failed)
    assert len(report.checks) >= 16


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
