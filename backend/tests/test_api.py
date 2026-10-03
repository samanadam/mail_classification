from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mailcat.api.auth import hash_password, throttle
from mailcat.ingest.attachments import store_bytes
from mailcat.models import Attachment, Correction, Run, RunRequest, SettingsLog, User
from tests import factories as f

H = {"X-Requested-With": "mailcat"}


@pytest.fixture
def client(db):
    from mailcat.api.main import create_app

    db.add(User(username="eren", password_hash=hash_password("correct horse battery")))
    db.commit()
    throttle.reset("ip:testclient", "user:eren")
    c = TestClient(create_app())
    r = c.post("/api/auth/login", json={"username": "eren", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 200
    return c


def test_auth_required_csrf_and_throttle(db):
    from mailcat.api.main import create_app

    db.add(User(username="eren", password_hash=hash_password("correct horse battery")))
    db.commit()
    throttle.reset("ip:testclient", "user:eren")
    c = TestClient(create_app())
    assert c.get("/api/mails").status_code == 401
    assert c.post("/api/auth/login", json={"username": "eren", "password": "x"}).status_code == 403  # no header
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "eren", "password": "wrong"}, headers=H).status_code == 401
    r = c.post("/api/auth/login", json={"username": "eren", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 429
    throttle.reset("ip:testclient", "user:eren")
    r = c.post("/api/auth/login", json={"username": "eren", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert c.get("/api/auth/me").json() == {"username": "eren"}
    assert c.post("/api/auth/logout", headers=H).status_code == 200
    assert c.get("/api/auth/me").status_code == 401


def test_mail_list_filters_search_and_detail(client, db):
    acc = f.account(db)
    fin = f.category(db, "Finance")
    a = f.mail(db, acc, subject="Fatura ödemesi alındı", body_text="Turkcell faturanız", category_id=fin.id,
               seen=False)
    f.mail(db, acc, subject="Hello friend", body_text="lunch?", seen=True)
    b = f.mail(db, acc, subject="Phish", malicious=True)
    db.commit()
    page = client.get("/api/mails").json()
    assert page["total"] == 3
    assert [m["id"] for m in client.get(f"/api/mails?category_id={fin.id}").json()["items"]] == [a.id]
    assert client.get("/api/mails?seen=false").json()["total"] == 2
    assert [m["id"] for m in client.get("/api/mails?malicious=true").json()["items"]] == [b.id]
    assert [m["id"] for m in client.get("/api/mails?q=turkcell").json()["items"]] == [a.id]
    assert [m["id"] for m in client.get("/api/mails?q=fatura").json()["items"]] == [a.id]
    detail = client.get(f"/api/mails/{a.id}").json()
    assert detail["subject"] == "Fatura ödemesi alındı" and detail["category_id"] == fin.id
    assert client.get("/api/mails/999999").status_code == 404


def test_correction_and_review_queue(client, db):
    acc = f.account(db)
    und = f.category(db, "Undecided")
    m = f.mail(db, acc, category_id=und.id, decided_by="threshold", confidence=0.4)
    low = f.mail(db, acc, category_id=f.category(db, "Personal").id, decided_by="model", confidence=0.65)
    f.mail(db, acc, category_id=f.category(db, "Personal").id, decided_by="model", confidence=0.95)
    db.commit()
    review = {x["id"] for x in client.get("/api/mails?review=true").json()["items"]}
    assert review == {m.id, low.id}
    work = f.category(db, "Work")
    assert client.patch(f"/api/mails/{m.id}", json={"category_id": work.id}).status_code == 403  # CSRF
    r = client.patch(f"/api/mails/{m.id}", json={"category_id": work.id}, headers=H)
    assert r.status_code == 200
    db.expire_all()
    corr = db.scalar(select(Correction))
    assert corr.source == "dashboard" and corr.old_category_id == und.id and corr.new_category_id == work.id
    assert m.id not in {x["id"] for x in client.get("/api/mails?review=true").json()["items"]}
    assert client.patch(f"/api/mails/{m.id}", json={"keep": True}, headers=H).json()["keep"] is True


def test_attachment_download_rules(client, db):
    acc = f.account(db)
    good = f.mail(db, acc)
    bad = f.mail(db, acc, malicious=True)
    name, digest = store_bytes(b"%PDF-1.7 hello")
    pdf = Attachment(mail_id=good.id, filename="../../etc/passwd statement.pdf", stored_name=name, sha256=digest,
                     size=14, scan_status="clean")
    name2, digest2 = store_bytes(b"MZ")
    exe = Attachment(mail_id=good.id, filename="x.exe", stored_name=name2, sha256=digest2, risky=True,
                     scan_status="clean")
    name3, digest3 = store_bytes(b"x")
    on_bad = Attachment(mail_id=bad.id, filename="a.pdf", stored_name=name3, sha256=digest3, scan_status="clean")
    name4, digest4 = store_bytes(b"unscanned")
    unscanned = Attachment(mail_id=good.id, filename="b.pdf", stored_name=name4, sha256=digest4, scan_status="skipped")
    db.add_all([pdf, exe, on_bad, unscanned])
    db.commit()
    r = client.get(f"/api/mails/attachments/{pdf.id}/download")
    assert r.status_code == 200 and r.content == b"%PDF-1.7 hello"
    assert r.headers["content-type"] == "application/octet-stream"
    assert "passwd" in r.headers["content-disposition"] and "/" not in r.headers["content-disposition"].split("=")[-1]
    assert client.get(f"/api/mails/attachments/{exe.id}/download").status_code == 409
    assert client.get(f"/api/mails/attachments/{exe.id}/download?confirm_risky=true").status_code == 200
    assert client.get(f"/api/mails/attachments/{on_bad.id}/download").status_code == 403
    assert client.get(f"/api/mails/attachments/{unscanned.id}/download").status_code == 409
    assert client.get(f"/api/mails/attachments/{unscanned.id}/download?confirm_risky=true").status_code == 200


def test_password_change_signs_out_other_sessions(client, db):
    from mailcat.api.main import create_app

    other = TestClient(create_app())
    assert other.post("/api/auth/login", json={"username": "eren", "password": "correct horse battery"},
                      headers=H).status_code == 200
    r = client.post("/api/auth/password", headers=H,
                    json={"current_password": "correct horse battery", "new_password": "a brand new passphrase"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401


def test_html_view_is_sanitized(client, db):
    acc = f.account(db)
    m = f.mail(db, acc, body_html='<p>Hi</p><script>steal()</script><img src="https://t.example/x.gif">')
    db.commit()
    data = client.get(f"/api/mails/{m.id}/html").json()
    assert "<script" not in data["html"] and data["blocked_images"] == 1
    client.put("/api/settings", json={"html_view_enabled": False}, headers=H)
    assert client.get(f"/api/mails/{m.id}/html").status_code == 403


def test_settings_validation_and_log(client, db):
    r = client.put("/api/settings", json={"confidence_threshold": 0.7, "run_time": "05:30"}, headers=H)
    assert r.status_code == 200 and r.json()["values"]["confidence_threshold"] == 0.7
    assert client.put("/api/settings", json={"confidence_threshold": 2, "run_time": "06:00"},
                      headers=H).status_code == 400
    assert client.get("/api/settings").json()["values"]["run_time"] == "05:30"  # nothing half-applied
    logs = db.scalars(select(SettingsLog).where(SettingsLog.key == "confidence_threshold")).all()
    assert len(logs) == 1 and logs[0].old_value == 0.6 and logs[0].new_value == 0.7
    assert logs[0].changed_by == "eren"


def test_shorter_delete_timer_needs_confirmation(client, db):
    acc = f.account(db, deletion_enabled=True, deletion_dry_run=False)
    personal = f.category(db, "Personal")  # 30 days
    for _ in range(3):
        f.mail(db, acc, category_id=personal.id, age_days=10)
    db.commit()
    impact = client.get(f"/api/categories/{personal.id}/impact?mailbox_delete_days=7&local_delete_days=7").json()
    assert impact["additional"] == 3
    r = client.patch(f"/api/categories/{personal.id}", json={"mailbox_delete_days": 7}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["impact"]["additional"] == 3
    r = client.patch(f"/api/categories/{personal.id}", json={"mailbox_delete_days": 7, "confirm_impact": True},
                     headers=H)
    assert r.status_code == 200 and r.json()["mailbox_delete_days"] == 7
    # lengthening never needs confirmation
    assert client.patch(f"/api/categories/{personal.id}", json={"mailbox_delete_days": 60},
                        headers=H).status_code == 200
    preview = client.get("/api/deletions/preview").json()
    assert preview["live_count"] == 0


def test_category_create_rename_merge_delete(client, db):
    acc = f.account(db)
    r = client.post("/api/categories", json={"name": "Kids School", "description": "school mail"}, headers=H)
    new_id = r.json()["id"]
    assert client.post("/api/categories", json={"name": "kids school"}, headers=H).status_code == 409
    assert client.post("/api/categories", json={"name": "a/b"}, headers=H).status_code == 400
    m = f.mail(db, acc, category_id=new_id)
    db.commit()
    assert client.delete(f"/api/categories/{new_id}", headers=H).status_code == 409
    assert client.patch(f"/api/categories/{new_id}", json={"name": "School"}, headers=H).json()["name"] == "School"
    personal = f.category(db, "Personal")
    assert client.post(f"/api/categories/{new_id}/merge", json={"into_id": personal.id},
                       headers=H).json()["mails_moved"] == 1
    db.expire_all()
    assert db.get(type(m), m.id).category_id == personal.id
    und = f.category(db, "Undecided")
    assert client.post(f"/api/categories/{und.id}/merge", json={"into_id": personal.id},
                       headers=H).status_code == 400


def test_going_live_requires_a_dry_run(client, db):
    r = client.post("/api/accounts", json={"name": "gm", "provider": "gmail", "email": "me@gmail.com",
                                          "password": "app-password"}, headers=H)
    acc_id = r.json()["id"]
    assert r.json()["writeback_enabled"] is False and r.json()["deletion_enabled"] is False
    assert "app-password" not in r.text
    r = client.patch(f"/api/accounts/{acc_id}", json={"writeback_enabled": True, "writeback_dry_run": False},
                     headers=H)
    assert r.status_code == 409
    assert client.patch(f"/api/accounts/{acc_id}", json={"writeback_enabled": True}, headers=H).status_code == 200
    db.add(Run(kind="scheduled", status="ok", started_at=datetime.now(UTC), finished_at=datetime.now(UTC)))
    db.commit()
    r = client.patch(f"/api/accounts/{acc_id}", json={"writeback_dry_run": False}, headers=H)
    assert r.status_code == 200 and r.json()["writeback_dry_run"] is False
    assert client.post("/api/accounts", json={"name": "x", "provider": "custom", "email": "a@b.c",
                                             "host": "mail.example.com", "security": "plain",
                                             "password": "p"}, headers=H).status_code == 400


def test_rules_api_and_sync_now(client, db):
    acc = f.account(db)
    f.mail(db, acc, from_address="news@shop.com", subject="Sale")
    db.commit()
    fin = f.category(db, "Promotions")
    cond = [{"field": "from_domain", "op": "domain_in", "value": ["shop.com"]}]
    assert client.post("/api/rules/test", json={"conditions": cond}, headers=H).json()["matched"] == 1
    r = client.post("/api/rules", json={"name": "shop", "conditions": cond, "category_id": fin.id}, headers=H)
    assert r.status_code == 200
    assert client.post("/api/rules", json={"name": "bad", "conditions": [{"field": "subject", "op": "regex",
                                                                          "value": "("}],
                                           "category_id": fin.id}, headers=H).status_code == 400
    assert client.post("/api/runs/sync", headers=H).status_code == 200
    assert client.post("/api/runs/sync", headers=H).status_code == 200
    assert len(db.scalars(select(RunRequest)).all()) == 1  # duplicate clicks queue one run
    status = client.get("/api/status").json()
    assert status["sync_requested"] is True and status["next_run_at"]
