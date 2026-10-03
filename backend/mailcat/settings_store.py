"""Global settings: defaults, validation, reads and logged writes.

Every value lives in the `settings` table. The worker reads them at the start of each run, so a
change takes effect at the next run without a restart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailcat.models import Setting, SettingsLog


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class SettingSpec:
    default: Any
    kind: str  # int | float | bool | str | time | timezone | str_list | dict
    label: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    group: str = "General"


SPECS: dict[str, SettingSpec] = {
    "run_time": SettingSpec("04:00", "time", "Daily run time", "HH:MM, local time", group="Schedule"),
    "timezone": SettingSpec("Europe/Istanbul", "timezone", "Time zone", group="Schedule"),
    "runs_paused": SettingSpec(
        False, "bool", "Pause scheduled runs", "Sync now still works while paused", group="Schedule"
    ),
    "confidence_threshold": SettingSpec(
        0.6, "float", "Confidence threshold", "Below this the mail goes to Undecided", 0.0, 1.0,
        group="Classification",
    ),
    "thread_inheritance": SettingSpec(
        True, "bool", "Replies inherit thread category", group="Classification"
    ),
    "body_max_chars": SettingSpec(
        3000, "int", "Body characters given to the model", min=200, max=20000, group="Classification"
    ),
    "needs_action_threshold": SettingSpec(
        0.6, "float", "Needs-action threshold", min=0.0, max=1.0, group="Classification"
    ),
    "model_phishing_flags_malicious": SettingSpec(
        False, "bool", "Let the model's phishing score set `malicious`",
        "Off by default: the base model over-flags marketing and bank mail. Enable after phase 0 shows it works",
        group="Security",
    ),
    "phishing_threshold": SettingSpec(
        0.9, "float", "Model phishing threshold", "Used only when the option above is on", 0.5, 1.0,
        group="Security",
    ),
    "daily_delete_cap": SettingSpec(
        200, "int", "Daily deletion cap", "A run that wants to delete more stops and alerts",
        min=0, max=100000, group="Retention",
    ),
    "move_batch_limit": SettingSpec(
        500, "int", "Moves per account per run", "Larger backlogs are moved over several runs",
        min=0, max=100000, group="Write-back",
    ),
    "protect_flagged": SettingSpec(True, "bool", "Never delete starred/flagged mail", group="Retention"),
    "protect_undecided": SettingSpec(True, "bool", "Never delete Undecided mail", group="Retention"),
    "protect_needs_action": SettingSpec(False, "bool", "Never delete needs-action mail", group="Retention"),
    "protect_unread": SettingSpec(False, "bool", "Never delete unread mail", group="Retention"),
    "protected_senders": SettingSpec(
        [], "str_list", "Protected senders", "Addresses or @domains that are never deleted",
        group="Retention",
    ),
    "backup_enabled": SettingSpec(True, "bool", "Nightly encrypted backup", group="Backups"),
    "backup_keep_days": SettingSpec(14, "int", "Backup lifetime (days)", min=1, max=365, group="Backups"),
    "spam_lookback_days": SettingSpec(
        30, "int", "Provider spam folder lookback (days)", min=1, max=365, group="Sync"
    ),
    "initial_sync_days": SettingSpec(
        14, "int", "First sync imports the last N days", "Older mail comes in through backfill",
        min=1, max=3650, group="Sync",
    ),
    "fetch_limit_per_folder": SettingSpec(
        5000, "int", "New mails fetched per folder per run", min=10, max=100000, group="Sync"
    ),
    "backfill_batch_size": SettingSpec(
        1000, "int", "Backfill mails per folder per run", "Older history imported newest first",
        min=10, max=50000, group="Sync",
    ),
    "backfill_pause_ms": SettingSpec(
        200, "int", "Pause between backfill fetches (ms)", "Throttle to stay under provider limits",
        min=0, max=10000, group="Sync",
    ),
    "html_view_enabled": SettingSpec(True, "bool", "Allow sanitized HTML view", group="Dashboard"),
    "blocklist_feeds": SettingSpec(
        {"urlhaus": True, "openphish": True, "phishtank": False},
        "dict", "Blocklist feeds", "Local copies refreshed each run", group="Security",
    ),
    "phishtank_url": SettingSpec(
        "", "str", "PhishTank feed URL", "Needs a registered app key in the URL", group="Security"
    ),
    "alert_on_success": SettingSpec(False, "bool", "Send an alert after every run", group="Alerts"),
    "alert_after_failures": SettingSpec(
        1, "int", "Alert after N failed syncs of an account", min=1, max=30, group="Alerts"
    ),
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate(key: str, value: Any) -> Any:
    spec = SPECS.get(key)
    if spec is None:
        raise SettingError(f"unknown setting {key!r}")
    kind = spec.kind
    try:
        if kind == "bool":
            if not isinstance(value, bool):
                raise SettingError("must be true or false")
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int | float) or int(value) != value:
                raise SettingError("must be a whole number")
            value = int(value)
        elif kind == "float":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SettingError("must be a number")
            value = float(value)
        elif kind == "time":
            if not isinstance(value, str) or not _TIME_RE.match(value):
                raise SettingError("must be HH:MM")
        elif kind == "timezone":
            if not isinstance(value, str):
                raise SettingError("must be a time zone name")
            ZoneInfo(value)
        elif kind == "str":
            if not isinstance(value, str):
                raise SettingError("must be text")
        elif kind == "str_list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise SettingError("must be a list of text values")
            value = sorted({v.strip().lower() for v in value if v.strip()})
        elif kind == "dict":
            if not isinstance(value, dict):
                raise SettingError("must be an object")
            if isinstance(spec.default, dict):
                unknown = set(value) - set(spec.default)
                if unknown:
                    raise SettingError(f"unknown keys: {', '.join(sorted(unknown))}")
                value = {**spec.default, **{k: bool(v) for k, v in value.items()}}
    except ZoneInfoNotFoundError as exc:
        raise SettingError(f"{key}: unknown time zone") from exc
    except SettingError as exc:
        raise SettingError(f"{key}: {exc}") from None
    if kind in ("int", "float"):
        if spec.min is not None and value < spec.min:
            raise SettingError(f"{key}: must be at least {spec.min}")
        if spec.max is not None and value > spec.max:
            raise SettingError(f"{key}: must be at most {spec.max}")
    return value


def get_all(session: Session) -> dict[str, Any]:
    stored = {row.key: row.value for row in session.scalars(select(Setting))}
    return {key: stored.get(key, spec.default) for key, spec in SPECS.items()}


def get(session: Session, key: str) -> Any:
    if key not in SPECS:
        raise SettingError(f"unknown setting {key!r}")
    row = session.get(Setting, key)
    return SPECS[key].default if row is None else row.value


def set_value(session: Session, key: str, value: Any, changed_by: str | None) -> Any:
    value = validate(key, value)
    row = session.get(Setting, key)
    old = SPECS[key].default if row is None else row.value
    if old == value:
        return value
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    log_change(session, key, old, value, changed_by)
    return value


def log_change(session: Session, key: str, old: Any, new: Any, changed_by: str | None) -> None:
    session.add(SettingsLog(key=key, old_value=old, new_value=new, changed_by=changed_by))
