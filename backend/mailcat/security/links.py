"""Offline link heuristics. Never resolves or visits a URL."""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import tldextract

from mailcat.security.blocklists import Blocklist

# Offline suffix list only: tldextract must never fetch at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

SHORTENERS = {
    "bit.ly", "bitly.com", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly",
    "cutt.ly", "shorturl.at", "rb.gy", "t.ly", "s.id", "tiny.cc", "lnkd.in", "v.gd", "qrco.de",
    "shorte.st", "adf.ly", "bl.ink", "short.io", "soo.gd", "clck.ru", "u.to",
}
BRAND_DOMAINS = {
    "google.com", "gmail.com", "apple.com", "icloud.com", "microsoft.com", "outlook.com", "live.com",
    "office.com", "paypal.com", "amazon.com", "amazon.com.tr", "yandex.com", "yandex.ru", "github.com",
    "linkedin.com", "facebook.com", "instagram.com", "netflix.com", "spotify.com", "whatsapp.com",
    "garantibbva.com.tr", "isbank.com.tr", "akbank.com", "yapikredi.com.tr", "ziraatbank.com.tr",
    "qnb.com.tr", "enpara.com", "denizbank.com", "halkbank.com.tr", "vakifbank.com.tr",
    "turkiye.gov.tr", "ptt.gov.tr", "trendyol.com", "hepsiburada.com", "n11.com", "dhl.com",
    "ups.com", "fedex.com", "yurticikargo.com", "araskargo.com.tr", "mngkargo.com.tr",
    "binance.com", "coinbase.com", "steampowered.com", "dropbox.com", "docusign.com",
}
_BRAND_LABELS = {_extract(d).domain: d for d in BRAND_DOMAINS}
_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "@": "a", "$": "s", "|": "l"})
_DOMAINISH = re.compile(r"(?i)^(?:https?://)?(?:www\.)?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)(?:[/:?#].*)?$")


@dataclass
class LinkVerdict:
    verdict: str = "ok"  # ok | suspicious | malicious
    reasons: list[str] = field(default_factory=list)

    def flag(self, level: str, reason: str) -> None:
        self.reasons.append(reason)
        if level == "malicious" or (level == "suspicious" and self.verdict == "ok"):
            self.verdict = level


def registered_domain(host: str) -> str:
    ext = _extract(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return host.lower()


def skeleton(label: str) -> str:
    """Map look-alike characters onto ASCII, so 'pаypa1' (Cyrillic а, digit 1) -> 'paypal'."""
    text = unicodedata.normalize("NFKD", label.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    homoglyphs = {"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
                  "ј": "j", "ԁ": "d", "ɡ": "g", "ı": "i", "ѕ": "s", "ԛ": "q", "ԝ": "w"}
    text = "".join(homoglyphs.get(c, c) for c in text).translate(_CONFUSABLE)
    return text.replace("rn", "m").replace("vv", "w")


def _distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def lookalike_of(host: str, include_subdomain: bool = True) -> str | None:
    """Return the brand domain this host imitates, if any.

    The same label under another suffix (amazon.de, google.com.tr) is a country site, not a
    lookalike; only altered labels (paypa1, g00gle, Cyrillic letters) and brand names placed in
    the subdomain of an unrelated domain count."""
    reg = registered_domain(host)
    if reg in BRAND_DOMAINS:
        return None
    ext = _extract(host)
    label = ext.domain.lower()
    if label.startswith("xn--"):
        try:
            label = label.encode().decode("idna")
        except UnicodeError:
            pass
    skel = skeleton(label)
    for brand_label, brand_domain in _BRAND_LABELS.items():
        if len(brand_label) < 4:
            continue
        if label != brand_label and (
            skel == brand_label or (len(brand_label) >= 6 and _distance(skel, brand_label) == 1)
        ):
            return brand_domain
        # brand in a subdomain of an unrelated domain: paypal.com.account-verify.xyz
        if include_subdomain and ext.subdomain and re.search(
            rf"(^|\.){re.escape(brand_label)}(\.|$)", ext.subdomain.lower()
        ):
            return brand_domain
    return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return bool(re.fullmatch(r"\d+", host)) or bool(re.fullmatch(r"0x[0-9a-f]+", host, re.I))


def _shown_domain(shown: str) -> str | None:
    shown = shown.strip()
    if not shown or " " in shown:
        return None
    match = _DOMAINISH.match(shown)
    if not match:
        return None
    candidate = match.group(1).lower()
    ext = _extract(candidate)
    return candidate if ext.suffix else None


def check_link(url: str, shown_text: str, blocklist: Blocklist | None) -> tuple[str, LinkVerdict]:
    """Returns (host, verdict)."""
    verdict = LinkVerdict()
    raw = url.strip()
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:", "file:")):
        verdict.flag("malicious", f"dangerous scheme {lowered.split(':', 1)[0]}:")
        return "", verdict
    try:
        parts = urlsplit(raw if "://" in raw else "http://" + raw)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        verdict.flag("suspicious", "unparsable URL")
        return "", verdict
    if not host:
        verdict.flag("suspicious", "URL without a host")
        return "", verdict
    if parts.scheme not in ("http", "https", ""):
        verdict.flag("suspicious", f"unusual scheme {parts.scheme}:")
    if "@" in (parts.netloc or ""):
        verdict.flag("suspicious", "credentials trick in URL (user@host)")
    if _is_ip(host):
        verdict.flag("suspicious", "raw IP address")
    if any(label.startswith("xn--") for label in host.split(".")):
        verdict.flag("suspicious", "punycode domain")
    elif any(ord(c) > 127 for c in host):
        verdict.flag("suspicious", "non-ASCII domain")
    reg = registered_domain(host)
    if host in SHORTENERS or reg in SHORTENERS:
        verdict.flag("suspicious", "URL shortener hides the destination")
    brand = lookalike_of(host)
    if brand:
        verdict.flag("suspicious", f"lookalike of {brand}")
    shown = _shown_domain(shown_text)
    if shown and registered_domain(shown) != reg:
        verdict.flag("suspicious", f"link text shows {shown} but goes to {host}")
    if blocklist is not None:
        hit = blocklist.lookup(raw)
        if hit:
            verdict.flag(*hit)
    return host, verdict
