from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("mailcat.run")


@dataclass
class RunContext:
    run_id: int | None
    settings: dict[str, Any]
    counts: Counter = field(default_factory=Counter)
    errors: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def error(self, step: str, message: str, account: str | None = None, alert: bool = True) -> None:
        entry = {"step": step, "message": message[:2000], "account": account, "alert": alert,
                 "at": datetime.now(UTC).isoformat()}
        self.errors.append(entry)
        log.error("[%s]%s %s", step, f" {account}:" if account else "", message)
        if alert:
            self.alerts.append(f"{step}{f' ({account})' if account else ''}: {message[:300]}")

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] += n
