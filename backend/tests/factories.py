from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mailcat.crypto import encrypt_credentials
from mailcat.models import Account, Category, Mail

_seq = itertools.count(1)


def account(session, **kw) -> Account:
    n = next(_seq)
    acc = Account(
        name=kw.pop("name", f"acct{n}"), provider=kw.pop("provider", "custom"),
        email=kw.pop("email", f"user{n}@localhost"), host=kw.pop("host", "localhost"),
        port=kw.pop("port", 3143), security=kw.pop("security", "plain"),
        username=kw.pop("username", f"user{n}@localhost"), auth_method="password",
        credentials_enc=encrypt_credentials({"password": kw.pop("password", "x")}),
        timer_overrides={}, folder_overrides={}, **kw,
    )
    session.add(acc)
    session.flush()
    return acc


def category(session, name: str) -> Category:
    return session.scalar(select(Category).where(Category.name == name))


def mail(session, acc: Account, **kw) -> Mail:
    n = next(_seq)
    m = Mail(
        account_id=acc.id, message_id=kw.pop("message_id", f"<m{n}@test>"),
        thread_id=kw.pop("thread_id", None) or f"<m{n}@test>", folder_role=kw.pop("folder_role", "inbox"),
        folder=kw.pop("folder", "INBOX"), uid=kw.pop("uid", n), uidvalidity=kw.pop("uidvalidity", 1),
        from_name=kw.pop("from_name", "Sender"), from_address=kw.pop("from_address", "sender@example.org"),
        to_addresses="me@example.org", cc_addresses="", subject=kw.pop("subject", f"Subject {n}"),
        received_at=kw.pop("received_at", datetime.now(UTC) - timedelta(days=kw.pop("age_days", 0))),
        body_text=kw.pop("body_text", "hello"), headers=kw.pop("headers", {}), malicious_reasons=[],
        **kw,
    )
    session.add(m)
    session.flush()
    return m
