"""Always-on, tiny scheduler. It never loads the model: each run is a child process, so the
model's memory is returned to the OS when the run exits.

- Daily run at `run_time` in `timezone` (both settings; changes apply without a restart).
  A run missed while the laptop was asleep starts as soon as it wakes.
- "Sync now" requests from the dashboard start within a few seconds.
- After the daily run: nightly encrypted backup (if enabled).
"""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from mailcat import settings_store
from mailcat.db import new_session
from mailcat.models import Run, RunRequest

log = logging.getLogger("mailcat.scheduler")

POLL_SECONDS = 5
EXIT_BUSY = 3  # `mailcat run` exit code when another run holds the lock
STALE_AFTER = timedelta(minutes=30)


def scheduled_time_for(day: datetime, settings: dict[str, Any]) -> datetime:
    tz = ZoneInfo(settings["timezone"])
    hour, minute = (int(x) for x in settings["run_time"].split(":"))
    local = day.astimezone(tz)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_run_at(settings: dict[str, Any], now: datetime) -> datetime | None:
    if settings.get("runs_paused"):
        return None
    today = scheduled_time_for(now, settings)
    return today if today > now else scheduled_time_for(now + timedelta(days=1), settings)


def due_scheduled_run(session, settings: dict[str, Any], now: datetime) -> bool:
    if settings.get("runs_paused"):
        return False
    slot = scheduled_time_for(now, settings)
    if now < slot:
        return False
    done = session.scalar(select(Run.id).where(Run.kind == "scheduled", Run.started_at >= slot).limit(1))
    return done is None


def mark_stale_runs(session) -> None:
    cutoff = datetime.now(UTC) - STALE_AFTER
    session.execute(update(Run).where(
        Run.status == "running",
        ((Run.heartbeat_at.is_(None)) & (Run.started_at < cutoff)) | (Run.heartbeat_at < cutoff),
    ).values(status="aborted", finished_at=datetime.now(UTC)))
    session.commit()


def _spawn(*args: str) -> int:
    cmd = [sys.executable, "-m", "mailcat.cli", *args]
    log.info("starting: %s", " ".join(cmd[2:]))
    return subprocess.call(cmd)  # noqa: S603 - fixed module, internal arguments


def backup_due(session, settings: dict[str, Any], now: datetime) -> bool:
    if not settings.get("backup_enabled"):
        return False
    from mailcat.ops.backup import last_backup_time

    last = last_backup_time()
    return last is None or now - last > timedelta(hours=20)


def loop() -> None:
    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    session = new_session()
    try:
        mark_stale_runs(session)
    finally:
        session.close()
    log.info("scheduler started")
    while not stop:
        session = new_session()
        try:
            settings = settings_store.get_all(session)
            now = datetime.now(UTC)
            request = session.scalar(select(RunRequest).where(RunRequest.picked_at.is_(None))
                                     .order_by(RunRequest.id).limit(1))
            if request is not None:
                run = Run(kind="manual", triggered_by=request.requested_by, status="running")
                session.add(run)
                session.flush()
                request.picked_at = now
                request.run_id = run.id
                session.commit()
                session.close()
                _spawn("run", "--kind", "manual", "--run-id", str(run.id))
                continue
            if due_scheduled_run(session, settings, now):
                session.close()
                if _spawn("run", "--kind", "scheduled") == EXIT_BUSY:
                    time.sleep(60)  # another run holds the lock; try again shortly
                    continue
                session = new_session()
                if backup_due(session, settings_store.get_all(session), datetime.now(UTC)):
                    _spawn("backup")
                continue
        except Exception:  # noqa: BLE001 - the scheduler must survive a database restart
            log.exception("scheduler tick failed")
            time.sleep(30)
        finally:
            session.close()
        time.sleep(POLL_SECONDS)
    log.info("scheduler stopped")
