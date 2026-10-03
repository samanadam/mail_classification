from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from mailcat import settings_store
from mailcat.api.auth import hash_password, throttle
from mailcat.models import SenderTimer, User
from mailcat.worker.policy import normalize_sender_pattern, plan_deletions, sender_matches
from tests import factories as f

H = {"X-Requested-With": "mailcat"}


def test_patterns():
    assert normalize_sender_pattern(" News@Shop.com ") == "news@shop.com"
    assert normalize_sender_pattern("shop.com") == "@shop.com"
    assert normalize_sender_pattern("@Mail.Shop.com") == "@mail.shop.com"
    for bad in ("", "@", "shop", "a@b@c.com", "a b@c.com"):
        with pytest.raises(ValueError):
            normalize_sender_pattern(bad)
    assert sender_matches("@shop.com", "deals@mail.shop.com")
    assert sender_matches("@shop.com", "x@shop.com")
    assert not sender_matches("@shop.com", "x@notshop.com")
    assert sender_matches("news@shop.com", "news@shop.com") and not sender_matches("news@shop.com", "a@shop.com")


def test_sender_timer_overrides_category_both_ways(db):
    acc = f.account(db, deletion_enabled=True, deletion_dry_run=False)
    fin = f.category(db, "Finance")  # never deletes
    promo = f.category(db, "Promotions")  # 3 / 3 days
    shop = f.mail(db, acc, from_address="deals@mail.shop.com", category_id=fin.id, age_days=5)
    keep_longer = f.mail(db, acc, from_address="news@brand.com", category_id=promo.id, age_days=10)
    other = f.mail(db, acc, from_address="x@other.com", category_id=promo.id, age_days=10)
    flagged = f.mail(db, acc, from_address="a@shop.com", category_id=fin.id, age_days=5, flagged=True)
    db.add_all([SenderTimer(pattern="@shop.com", mailbox_delete_days=3, local_delete_days=None),
                SenderTimer(pattern="news@brand.com", mailbox_delete_days=30, local_delete_days=30)])
    db.flush()
    plans = plan_deletions(db, settings_store.get_all(db), datetime.now(UTC))
    by = {(p.kind, p.mail.id): p for p in plans}
    assert by[("mailbox", shop.id)].rule == "sender @shop.com: delete mailbox after 3 days"
    assert ("local", shop.id) not in by  # sender sets no local timer; Finance says never
    assert not any(mid == keep_longer.id for _, mid in by)  # 30 days beats the 3-day category timer
    assert ("mailbox", other.id) in by and ("local", other.id) in by
    assert not any(mid == flagged.id for _, mid in by)  # protections still win


@pytest.fixture
def client(db):
    from mailcat.api.main import create_app

    db.add(User(username="eren", password_hash=hash_password("correct horse battery")))
    db.commit()
    throttle.reset("ip:testclient", "user:eren")
    c = TestClient(create_app())
    c.post("/api/auth/login", json={"username": "eren", "password": "correct horse battery"}, headers=H)
    return c


def test_sender_timer_api_requires_confirmation(client, db):
    acc = f.account(db, deletion_enabled=True, deletion_dry_run=False)
    f.mail(db, acc, from_address="deals@shop.com", category_id=f.category(db, "Finance").id, age_days=5)
    db.commit()
    body = {"pattern": "Shop.com", "mailbox_delete_days": 3}
    r = client.post("/api/sender-timers", json=body, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["impact"]["additional"] == 1
    r = client.post("/api/sender-timers", json={**body, "confirm_impact": True}, headers=H)
    assert r.status_code == 200 and r.json()["pattern"] == "@shop.com"
    tid = r.json()["id"]
    assert client.post("/api/sender-timers", json={**body, "confirm_impact": True}, headers=H).status_code == 409
    assert client.post("/api/sender-timers", json={"pattern": "x.com"}, headers=H).status_code == 400
    # lengthening needs no confirmation
    r = client.put(f"/api/sender-timers/{tid}", json={"pattern": "@shop.com", "mailbox_delete_days": 60}, headers=H)
    assert r.status_code == 200
    preview = client.get("/api/deletions/preview").json()
    assert preview["live_count"] == 0
    assert client.delete(f"/api/sender-timers/{tid}", headers=H).status_code == 200
    assert client.get("/api/sender-timers").json() == []
