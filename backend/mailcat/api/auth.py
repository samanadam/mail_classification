"""Dashboard login: argon2 password hashes, server-side sessions, login throttling."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mailcat.config import get_config
from mailcat.models import User, UserSession

COOKIE_NAME = "mailcat_session"
_hasher = PasswordHasher()
# Verified against when the username does not exist, so timing does not reveal valid names.
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    return _hasher.hash(password)


def verify_password(user: User | None, password: str) -> bool:
    try:
        return _hasher.verify(user.password_hash if user else _DUMMY_HASH, password) and user is not None
    except (VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(session: Session, user: User, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(UserSession(token_hash=_token_hash(token), user_id=user.id, expires_at=now + timedelta(
        hours=get_config().session_hours), user_agent=(user_agent or "")[:255]))
    user.last_login_at = now
    session.execute(delete(UserSession).where(UserSession.expires_at < now))
    return token


def user_for_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = session.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
    now = datetime.now(UTC)
    if row is None or row.expires_at < now:
        return None
    if (now - row.last_seen_at).total_seconds() > 300:
        row.last_seen_at = now
    return row.user


def end_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))


class LoginThrottle:
    """At most `limit` failed attempts per key within `window` seconds."""

    def __init__(self, limit: int = 5, window: int = 900) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        entries = self._failures[key]
        while entries and now - entries[0] > self.window:
            entries.popleft()
        return entries

    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._prune(k, now)) >= self.limit for k in keys)

    def fail(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._prune(key, now).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


throttle = LoginThrottle()
