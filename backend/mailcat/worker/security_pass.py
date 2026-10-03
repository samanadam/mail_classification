"""Step 3: link, header and attachment checks that set the `malicious` flag. All offline."""
from __future__ import annotations

from datetime import UTC, datetime

from mailcat.models import Mail
from mailcat.security.blocklists import Blocklist
from mailcat.security.headers import check_headers
from mailcat.security.links import check_link

# Several weak signals together are as telling as one strong one.
SOFT_SIGNALS_FOR_MALICIOUS = 3


def assess_mail(mail: Mail, blocklist: Blocklist | None) -> None:
    hard: list[str] = []
    soft: set[str] = set()
    for link in mail.links:
        host, verdict = check_link(link.url, link.shown_text, blocklist)
        link.host = host[:255]
        link.verdict = verdict.verdict
        link.reasons = verdict.reasons
        if verdict.verdict == "malicious":
            hard.extend(f"link {host or link.url[:80]}: {r}" for r in verdict.reasons)
        else:
            # Keep the kind of signal, not the host, so ten tracking links count once.
            soft.update(r.split(" of ")[0].split(" shows ")[0] for r in verdict.reasons)

    headers = check_headers(mail.headers or {}, mail.from_name or "", mail.from_address or "")
    hard.extend(headers.hard)
    soft.update(headers.soft)

    for att in mail.attachments:
        if att.scan_status == "infected":
            hard.append(f"attachment {att.filename}: {att.scan_detail or 'virus found'}")

    reasons = list(dict.fromkeys(hard))
    if not reasons and len(soft) >= SOFT_SIGNALS_FOR_MALICIOUS:
        reasons = sorted(soft)
    mail.malicious = bool(reasons)
    mail.malicious_reasons = reasons[:20]
    mail.security_checked_at = datetime.now(UTC)
    if mail.malicious:
        for att in mail.attachments:
            att.locked = True
