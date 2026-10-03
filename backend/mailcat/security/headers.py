"""Header checks from the receiving provider's Authentication-Results. No DNS lookups."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mailcat.security.links import BRAND_DOMAINS, lookalike_of, registered_domain

_RESULT_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.I)
_EMAIL_RE = re.compile(r"[\w.+\-]+@([\w\-]+(?:\.[\w\-]+)+)")
_BRAND_NAMES = {d.split(".")[0]: d for d in BRAND_DOMAINS if len(d.split(".")[0]) >= 4}


@dataclass
class HeaderVerdict:
    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)


def parse_auth_results(value: str) -> dict[str, str]:
    """Collapse one Authentication-Results header to a single result per method.
    Several DKIM signatures are common; one passing signature is enough."""
    found: dict[str, list[str]] = {}
    for method, result in _RESULT_RE.findall(value or ""):
        found.setdefault(method.lower(), []).append(result.lower())
    collapsed: dict[str, str] = {}
    for method, results in found.items():
        collapsed[method] = "pass" if "pass" in results else results[0]
    return collapsed


def check_headers(headers: dict, from_name: str, from_address: str) -> HeaderVerdict:
    verdict = HeaderVerdict()
    auth = headers.get("authentication-results") or []
    if isinstance(auth, str):
        auth = [auth]
    # Only the topmost header is trusted: it is added by our own provider. Lower ones can be
    # forged by the sender.
    if auth:
        results = parse_auth_results(auth[0])
        verdict.spf, verdict.dkim, verdict.dmarc = results.get("spf"), results.get("dkim"), results.get("dmarc")
    if verdict.spf in ("fail", "permerror"):
        verdict.hard.append(f"SPF {verdict.spf}")
    elif verdict.spf == "softfail":
        verdict.soft.append("SPF softfail")
    if verdict.dkim in ("fail", "permerror"):
        verdict.hard.append(f"DKIM {verdict.dkim}")
    if verdict.dmarc == "fail":
        verdict.hard.append("DMARC fail")

    sender_domain = from_address.rsplit("@", 1)[-1].lower() if "@" in from_address else ""
    sender_reg = registered_domain(sender_domain) if sender_domain else ""
    if sender_domain:
        brand = lookalike_of(sender_domain, include_subdomain=False)
        if brand:
            verdict.hard.append(f"sender domain imitates {brand}")
    name = (from_name or "").lower()
    if name and sender_reg:
        shown = _EMAIL_RE.search(name)
        if shown and registered_domain(shown.group(1)) != sender_reg:
            verdict.hard.append(f"display name shows {shown.group(0)} but sender is {from_address}")
        else:
            for brand_label, brand_domain in _BRAND_NAMES.items():
                if re.search(rf"\b{re.escape(brand_label)}\b", name) and \
                        registered_domain(brand_domain).split(".")[0] != sender_reg.split(".")[0]:
                    verdict.soft.append(f"display name mentions {brand_label} but sender is {sender_reg}")
                    break
    return verdict
