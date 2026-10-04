"""Fill a separate demo database with invented mail, for screenshots and trying the dashboard.

    MAILCAT_DATABASE_URL=postgresql+psycopg://.../mailcat_demo python scripts/demo_data.py

Everything here is fictional: people, companies and addresses use the reserved `.example` domain
(RFC 2606), so no address can belong to anyone. The script refuses to run unless the database
name ends in `_demo` and the database has no accounts yet, so it never touches a real install.
"""
from __future__ import annotations

import random
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import make_url

from mailcat.api.auth import hash_password
from mailcat.cli import cmd_migrate
from mailcat.config import get_config
from mailcat.crypto import encrypt_credentials
from mailcat.db import new_session
from mailcat.models import Account, ActionLog, Attachment, Category, Link, Mail, Run, SenderTimer, User
from mailcat.seed import seed

DEMO_USER = "demo"
DEMO_PASSWORD = "demo-password-123"  # noqa: S105 - local demo database only

ACCOUNTS = [
    ("Personal", "gmail", "alex.morgan@mail.example", "imap.mail.example"),
    ("iCloud", "icloud", "alex.m@cloud.example", "imap.cloud.example"),
    ("Studio", "custom", "alex@northwind-studio.example", "mail.northwind-studio.example"),
]

# (category, account index, from name, from address, subject, body, seen, flagged)
MAILS = [
    ("Work", 2, "Priya Shah", "priya@northwind-studio.example", "Sprint review moved to Thursday",
     "Hi Alex, the sprint review is now Thursday 14:00. Please bring the dashboard demo.", False, True),
    ("Work", 2, "Tom Becker", "tom@northwind-studio.example", "Re: API rate limits for the mobile app",
     "I agree we should cache the catalog responses. Can you draft the change?", True, False),
    ("Clients", 2, "Lena Ortiz", "lena@bluepeak-retail.example", "Feedback on the checkout redesign",
     "The new checkout flow tested well. Two small copy changes attached.", False, False),
    ("Clients", 2, "Marcus Hill", "marcus@harbor-logistics.example", "Invoice #2041 approved",
     "Thanks, invoice 2041 has been approved and will be paid this week.", True, False),
    ("Bank Statements", 0, "Fictional Bank", "statements@fictionalbank.example",
     "Your September card statement is ready", "Your statement for September is available in online banking.",
     True, False),
    ("Bank Statements", 0, "Fictional Bank", "statements@fictionalbank.example",
     "Your August card statement is ready", "Your statement for August is available in online banking.", True, False),
    ("Finance", 0, "City Power", "billing@citypower.example", "Electricity bill for September",
     "Your bill of 412.80 is due on 15 October.", False, False),
    ("Finance", 1, "Metro Telecom", "invoices@metrotelecom.example", "Your mobile invoice",
     "Your monthly invoice is attached.", True, False),
    ("Dev & Infra", 2, "CodeHost", "notifications@codehost.example", "[northwind/api] CI failed on main",
     "Build #812 failed: 2 tests failed in test_orders.py.", False, False),
    ("Dev & Infra", 2, "CloudHosting", "alerts@cloudhosting.example", "Disk usage above 85% on web-02",
     "web-02 reached 87% disk usage at 03:12 UTC.", True, False),
    ("Dev & Infra", 2, "Domain Registrar", "renewals@registrar.example", "northwind-studio.example renews in 30 days",
     "Auto-renewal is on. No action needed.", True, False),
    ("Security", 0, "Account Center", "no-reply@accounts.example", "New sign-in on a Linux device",
     "We noticed a new sign-in to your account. If this was you, you can ignore this mail.", False, False),
    ("Security", 1, "Password Vault", "security@vault.example", "Your 2FA backup codes were viewed",
     "Your backup codes were viewed from a trusted device.", True, False),
    ("Orders & Travel", 0, "SkyLine Air", "booking@skyline-air.example", "Your booking LX4821 is confirmed",
     "Flight to Lisbon, 18 October, seat 14C. Check-in opens 24 hours before departure.", False, True),
    ("Orders & Travel", 0, "ParcelGo", "tracking@parcelgo.example", "Your package is out for delivery",
     "Order 55-2190 will arrive today between 13:00 and 17:00.", True, False),
    ("Orders & Travel", 1, "Book Corner", "orders@bookcorner.example", "Order shipped: Designing Data-Intensive...",
     "Your order has shipped and should arrive in 2-3 days.", True, False),
    ("Personal", 1, "Sam Rivera", "sam.rivera@friends.example", "Dinner on Saturday?",
     "We're thinking of the new place by the river around 8. You in?", False, False),
    ("Personal", 0, "Mom", "jordan.morgan@family.example", "Photos from the weekend",
     "Here are the photos from the garden. The tomatoes did great this year!", True, True),
    ("Newsletters", 0, "Weekly Python", "editor@weeklypython.example", "Issue 312: faster startup, typed decorators",
     "This week: startup time improvements, typed decorators and a new packaging guide.", False, False),
    ("Newsletters", 0, "Frontend Digest", "hello@frontenddigest.example", "CSS anchor positioning is here",
     "Anchor positioning lands in all major browsers. Plus: view transitions in practice.", True, False),
    ("Newsletters", 1, "The Long Read", "letters@longread.example", "Sunday reading: the history of email",
     "From ARPANET to IMAP: a short history of the protocols we still use.", False, False),
    ("Promotions", 0, "ShopNest", "deals@shopnest.example", "Weekend sale: up to 50% off headphones",
     "Our biggest audio sale of the year ends Sunday.", False, False),
    ("Promotions", 0, "Urban Threads", "news@urbanthreads.example", "New autumn collection just dropped",
     "Discover the new autumn collection. Free shipping over 50.", False, False),
    ("Promotions", 1, "GameVault", "offers@gamevault.example", "3 games you might like, 70% off",
     "Based on your wishlist, these are on sale this week.", False, False),
    ("Promotions", 0, "FoodBox", "hello@foodbox.example", "Your first box is 60% off",
     "Come back and get 60% off your next box.", False, False),
    ("Social", 0, "ConnectIn", "notifications@connectin.example", "You appeared in 14 searches this week",
     "See who's looking at your profile.", False, False),
    ("Social", 1, "PhotoShare", "no-reply@photoshare.example", "Sam Rivera tagged you in a photo",
     "Sam Rivera tagged you in a photo from Saturday.", True, False),
    ("Undecided", 0, "Community Garden", "board@garden-club.example", "Minutes from the September meeting",
     "Attached are the minutes. Next meeting: 12 October.", False, False),
    ("Undecided", 1, "R. Novak", "r.novak@mail.example", "Quick question about the old project",
     "Do you still have the design files from 2022? No rush.", False, False),
    ("Spam & Malicious", 0, "Fictional Bank Support", "support@fictiona1bank.example",
     "Urgent: your account will be suspended", "Verify your identity within 24 hours to avoid suspension.",
     False, False),
    ("Spam & Malicious", 0, "Parcel Service", "notice@parcel-redelivery.example", "Delivery failed: pay 1.99 fee",
     "Your parcel could not be delivered. Pay the redelivery fee to reschedule.", False, False),
]

PHISHING_HTML = """<p>Dear customer,</p>
<p>We detected unusual activity. <b>Verify your identity within 24 hours</b> or your account will be
suspended.</p>
<p><a href="http://fictiona1bank.example.login-check.example/verify">https://www.fictionalbank.example/secure</a></p>
<img src="https://tracker.example/pixel.gif" alt="logo">
<p>Fictional Bank Security Team</p>"""


def main() -> None:
    url = make_url(get_config().database_url)
    if not (url.database or "").endswith("_demo"):
        raise SystemExit("refusing to run: the database name must end in _demo")
    cmd_migrate(None)  # type: ignore[arg-type]
    session = new_session()
    if session.scalar(select(Account.id).limit(1)) is not None:
        raise SystemExit("refusing to run: this database already has accounts")
    seed(session)
    session.add(User(username=DEMO_USER, password_hash=hash_password(DEMO_PASSWORD)))
    rng = random.Random(7)
    now = datetime.now(UTC).replace(second=0, microsecond=0)

    accounts = []
    for name, provider, email, host in ACCOUNTS:
        acc = Account(name=name, provider=provider, email=email, host=host, port=993, security="ssl",
                      username=email, auth_method="password",
                      credentials_enc=encrypt_credentials({"password": secrets.token_urlsafe(12)}),
                      writeback_enabled=True, writeback_dry_run=name != "Personal", deletion_enabled=name == "Personal",
                      deletion_dry_run=True, timer_overrides={}, folder_overrides={},
                      last_sync_at=now - timedelta(hours=2), last_sync_ok=True, consecutive_failures=0)
        session.add(acc)
        accounts.append(acc)
    session.flush()
    cats = {c.name: c for c in session.scalars(select(Category))}

    run = Run(kind="scheduled", triggered_by="scheduler", started_at=now - timedelta(hours=2, minutes=4),
              finished_at=now - timedelta(hours=2), status="ok",
              counts={"fetched": len(MAILS), "classified": len(MAILS), "moved": 9, "deletes_planned_dry_run": 4})
    session.add(run)
    session.add(Run(kind="manual", triggered_by=DEMO_USER, started_at=now - timedelta(days=1, hours=3),
                    finished_at=now - timedelta(days=1, hours=3) + timedelta(minutes=3), status="ok",
                    counts={"fetched": 6, "classified": 6, "moved": 2}))
    session.flush()

    for i, (cat_name, acc_idx, from_name, from_addr, subject, body, seen, flagged) in enumerate(MAILS):
        cat = cats[cat_name]
        received = now - timedelta(hours=3 + i * 7 + rng.randint(0, 5), minutes=rng.randint(0, 59))
        undecided = cat_name == "Undecided"
        top = rng.uniform(0.38, 0.52) if undecided else rng.uniform(0.74, 0.97)
        others = [c for c in cats if c not in (cat_name, "Undecided")]
        rng.shuffle(others)
        if undecided:
            probs = {others[0]: round(top, 2), others[1]: round(top - 0.08, 2), others[2]: 0.1}
        else:
            rest = 1 - top
            probs = {cat_name: round(top, 2), others[0]: round(rest * 0.6, 2), others[1]: round(rest * 0.3, 2)}
        malicious = cat_name == "Spam & Malicious"
        by_rule = cat_name in ("Bank Statements", "Dev & Infra") or malicious
        mail = Mail(
            account_id=accounts[acc_idx].id, message_id=f"<demo-{i}@mailcat.example>",
            thread_id=f"<demo-{i}@mailcat.example>", folder_role="sorted" if i % 3 else "inbox",
            folder=f"Sorted/{cat_name}" if i % 3 else "INBOX", uid=1000 + i, uidvalidity=1,
            from_name=from_name, from_address=from_addr, to_addresses=accounts[acc_idx].email,
            subject=subject, received_at=received, size=rng.randint(4_000, 90_000), body_text=body,
            body_html=PHISHING_HTML if from_addr.startswith("support@fictiona1bank") else None,
            headers={"from": f"{from_name} <{from_addr}>", "subject": subject},
            seen=seen, flagged=flagged, category_id=cat.id, confidence=round(top, 2),
            decided_by="security" if malicious else ("rule" if by_rule else ("threshold" if undecided else "model")),
            decision_detail=("lookalike of fictionalbank.example; SPF fail" if malicious else
                             "sender rule" if by_rule else None),
            probabilities=None if by_rule else probs, classified_at=received + timedelta(minutes=5),
            classifier_version="laya-multilingual", malicious=malicious,
            malicious_reasons=(["Sender domain fictiona1bank.example looks like fictionalbank.example",
                                "SPF failed", "Link text shows a different domain than its destination"]
                               if malicious else []),
            phishing_score=0.93 if malicious else round(rng.uniform(0.01, 0.2), 2),
            needs_action=cat_name in ("Clients", "Finance") and not seen,
            security_checked_at=received + timedelta(minutes=5),
            moved_at=received + timedelta(days=1) if i % 3 else None,
        )
        session.add(mail)
        session.flush()
        if malicious:
            session.add(Link(mail_id=mail.id, shown_text="https://www.fictionalbank.example/secure",
                             url="http://fictiona1bank.example.login-check.example/verify",
                             host="fictiona1bank.example.login-check.example", verdict="malicious",
                             reasons=["lookalike of fictionalbank.example", "text and destination differ"]))
        if cat_name == "Clients" and "checkout" in subject:
            session.add(Attachment(mail_id=mail.id, filename="checkout-copy-changes.pdf",
                                   stored_name=secrets.token_hex(16), content_type="application/pdf", size=182_311,
                                   sha256=secrets.token_hex(32), scan_status="clean"))
        if cat_name == "Spam & Malicious" and "Delivery" in subject:
            session.add(Attachment(mail_id=mail.id, filename="delivery_label.pdf.exe",
                                   stored_name=secrets.token_hex(16),
                                   content_type="application/octet-stream", size=48_128, sha256=secrets.token_hex(32),
                                   scan_status="infected", scan_detail="Win.Trojan.Agent-Demo", risky=True,
                                   locked=True))
        if i % 3:
            session.add(ActionLog(run_id=run.id, account_id=mail.account_id, mail_id=mail.id,
                                  message_id=mail.message_id, action="move", from_folder="INBOX",
                                  to_folder=mail.folder, rule=f"{cat_name}: move after 1 day(s)",
                                  sender=from_addr, subject=subject, mail_date=received,
                                  dry_run=mail.account_id != accounts[0].id, verified=True))
    for i, (_name, addr, subject) in enumerate([
        ("ShopNest", "deals@shopnest.example", "Flash sale: 24 hours only"),
        ("Urban Threads", "news@urbanthreads.example", "Last chance: summer clearance"),
        ("GameVault", "offers@gamevault.example", "Your wishlist is on sale"),
        ("FoodBox", "hello@foodbox.example", "We miss you! 50% off"),
    ]):
        session.add(ActionLog(run_id=run.id, account_id=accounts[0].id, message_id=f"<old-promo-{i}@mailcat.example>",
                              action="delete", from_folder="Sorted/Promotions", to_folder="Trash",
                              rule="Promotions: delete after 30 days", sender=addr, subject=subject,
                              mail_date=now - timedelta(days=31 + i), dry_run=True))
    session.add(SenderTimer(pattern="@foodbox.example", mailbox_delete_days=7, local_delete_days=30, enabled=True,
                            note="daily promos"))
    session.commit()
    session.close()
    print(f"demo data ready: log in as {DEMO_USER} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
