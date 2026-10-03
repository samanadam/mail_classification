"""Discord webhook alerts. Messages carry counts and error summaries, never mail content."""
from __future__ import annotations

import logging

import httpx

from mailcat.config import get_config

log = logging.getLogger(__name__)
MAX_LEN = 1900


def send_alert(title: str, lines: list[str]) -> bool:
    url = get_config().discord_webhook_url
    if not url:
        log.warning("alert (no webhook configured): %s %s", title, lines)
        return False
    body = f"**{title}**\n" + "\n".join(f"- {line}" for line in lines)
    if len(body) > MAX_LEN:
        body = body[:MAX_LEN - 20] + "\n… (truncated)"
    try:
        response = httpx.post(url, json={"content": body, "allowed_mentions": {"parse": []}}, timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("could not send alert: %s", exc)
        return False
    return True
