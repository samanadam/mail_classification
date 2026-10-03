"""Local copies of public phishing/malware URL feeds.

Feeds are downloaded once per run (step 1). Only the feed is fetched; no URL from any mail is
ever sent anywhere. A failed refresh keeps the previous copy.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from mailcat.config import get_config

log = logging.getLogger(__name__)

FEEDS: dict[str, str] = {
    # CC0, malware distribution URLs currently online.
    "urlhaus": "https://urlhaus.abuse.ch/downloads/text_online/",
    # Community feed, free for non-commercial use.
    "openphish": "https://openphish.com/feed.txt",
}
# Kinds decide how a host-only hit is treated: phishing hosts are usually dedicated, malware
# URLs are often on compromised legitimate hosts.
FEED_KIND = {"urlhaus": "malware", "openphish": "phishing", "phishtank": "phishing"}


def normalize_url(url: str) -> str | None:
    url = url.strip().strip("\"'<>")
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if not host:
        return None
    port = ""
    try:
        if parts.port and parts.port not in (80, 443):
            port = f":{parts.port}"
    except ValueError:
        return None
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host + port, path, parts.query, ""))


def strip_query(normalized: str) -> str:
    parts = urlsplit(normalized)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def host_of(normalized: str) -> str:
    return (urlsplit(normalized).hostname or "").lower()


@dataclass
class Blocklist:
    urls: dict[str, str] = field(default_factory=dict)  # normalized url -> feed
    paths: dict[str, str] = field(default_factory=dict)  # url without query -> feed
    phishing_hosts: dict[str, str] = field(default_factory=dict)  # host -> feed
    loaded_feeds: list[str] = field(default_factory=list)

    def add(self, feed: str, raw_url: str) -> None:
        norm = normalize_url(raw_url)
        if not norm:
            return
        self.urls.setdefault(norm, feed)
        self.paths.setdefault(strip_query(norm), feed)
        if FEED_KIND.get(feed) == "phishing":
            self.phishing_hosts.setdefault(host_of(norm), feed)

    def lookup(self, url: str) -> tuple[str, str] | None:
        """Returns (level, reason) with level 'malicious' or 'suspicious', or None."""
        norm = normalize_url(url)
        if not norm:
            return None
        feed = self.urls.get(norm) or self.paths.get(strip_query(norm))
        if feed:
            return "malicious", f"listed in {feed}"
        host = host_of(norm)
        if host in self.phishing_hosts:
            return "suspicious", f"host listed in {self.phishing_hosts[host]}"
        return None

    def __len__(self) -> int:
        return len(self.urls)


def _dir() -> Path:
    path = get_config().blocklists_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_feed(name: str, text: str) -> list[str]:
    if name == "phishtank":
        rows = csv.DictReader(io.StringIO(text))
        return [row["url"] for row in rows if row.get("url")]
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def refresh(enabled: dict[str, bool], phishtank_url: str = "", timeout: float = 60.0) -> dict[str, str]:
    """Download enabled feeds. Returns {feed: "ok" | "error: ..." | "skipped"}."""
    feeds = dict(FEEDS)
    if phishtank_url:
        feeds["phishtank"] = phishtank_url
    status: dict[str, str] = {}
    directory = _dir()
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "mailcat-blocklist-sync/1.0"}) as client:
        for name, url in feeds.items():
            if not enabled.get(name):
                status[name] = "skipped"
                continue
            try:
                response = client.get(url)
                response.raise_for_status()
                entries = _parse_feed(name, response.text)
                if not entries:
                    raise ValueError("feed was empty")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("blocklist %s refresh failed: %s", name, exc)
                status[name] = f"error: {exc}"
                continue
            target = directory / f"{name}.txt"
            tmp = target.with_suffix(".tmp")
            tmp.write_text("\n".join(entries), encoding="utf-8")
            os.replace(tmp, target)
            meta = {"updated_at": datetime.now(UTC).isoformat(), "entries": len(entries)}
            (directory / f"{name}.json").write_text(json.dumps(meta), encoding="utf-8")
            status[name] = "ok"
    return status


def load(enabled: dict[str, bool] | None = None) -> Blocklist:
    blocklist = Blocklist()
    directory = _dir()
    for path in sorted(directory.glob("*.txt")):
        name = path.stem
        if enabled is not None and not enabled.get(name):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            blocklist.add(name, line)
        blocklist.loaded_feeds.append(name)
    return blocklist


def status() -> dict[str, dict]:
    result: dict[str, dict] = {}
    directory = get_config().blocklists_path
    if not directory.is_dir():  # the API mounts /data read-only and must not create it
        return result
    for meta_path in directory.glob("*.json"):
        try:
            result[meta_path.stem] = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return result
