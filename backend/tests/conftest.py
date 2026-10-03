"""Test setup.

Database tests need PostgreSQL (MAILCAT_TEST_DATABASE_URL, default: the docker-compose.test.yml
service on localhost:55432). IMAP tests need GreenMail (MAILCAT_TEST_IMAP_HOST/PORT, default
localhost:3143). Tests that need a missing service are skipped, never silently passed.
"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="mailcat-test-"))
os.environ.setdefault("MAILCAT_TEST_DATABASE_URL",
                      "postgresql+psycopg://mailcat:mailcat@localhost:55432/mailcat_test")
os.environ["MAILCAT_DATABASE_URL"] = os.environ["MAILCAT_TEST_DATABASE_URL"]
os.environ["MAILCAT_SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["MAILCAT_DATA_DIR"] = str(_TMP)
os.environ["MAILCAT_BACKUP_DIR"] = str(_TMP / "backup")
os.environ["MAILCAT_CLAMAV_MODE"] = "off"
os.environ["MAILCAT_DISCORD_WEBHOOK_URL"] = ""
os.environ["MAILCAT_CLASSIFIER"] = "rules_only"

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from mailcat.config import get_config  # noqa: E402
from mailcat.db import new_session, reset_engine_cache  # noqa: E402

get_config.cache_clear()

IMAP_HOST = os.environ.get("MAILCAT_TEST_IMAP_HOST", "localhost")
IMAP_PORT = int(os.environ.get("MAILCAT_TEST_IMAP_PORT", "3143"))

TABLES = ("user_sessions", "users", "sender_timers", "corrections", "actions_log", "links", "attachments",
          "tombstones", "mails", "folder_state", "rules", "run_requests", "runs", "settings_log", "settings",
          "accounts", "categories")


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def database() -> str:
    url = make_url(os.environ["MAILCAT_DATABASE_URL"])
    if not _reachable(url.host or "localhost", url.port or 5432):
        pytest.skip("PostgreSQL test database not reachable")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    reset_engine_cache()
    from mailcat.cli import cmd_migrate

    cmd_migrate(None)  # type: ignore[arg-type]
    return str(url)


@pytest.fixture
def db(database):
    """A clean, seeded database per test."""
    from mailcat.db import get_engine
    from mailcat.seed import seed

    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    session = new_session()
    seed(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def imap_server():
    if not _reachable(IMAP_HOST, IMAP_PORT):
        pytest.skip("GreenMail IMAP test server not reachable")
    return IMAP_HOST, IMAP_PORT
