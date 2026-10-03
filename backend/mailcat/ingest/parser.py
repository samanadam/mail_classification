"""Turn raw RFC 822 bytes into the fields we store. Pure functions, no I/O."""
from __future__ import annotations

import email
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup

# Headers kept for classification and security checks. Multi-valued ones are stored as lists.
KEPT_HEADERS = (
    "list-unsubscribe", "list-unsubscribe-post", "list-id", "precedence", "auto-submitted",
    "authentication-results", "arc-authentication-results", "received-spf", "dkim-signature",
    "return-path", "reply-to", "sender", "x-mailer", "x-spam-flag", "x-spam-status",
    "x-priority", "importance", "x-gm-labels", "content-language",
)
_MULTI = {"authentication-results", "arc-authentication-results", "received-spf", "dkim-signature"}

_URL_RE = re.compile(r"""(?i)\b((?:https?://|www\.)[^\s<>"'()\[\]{}]+)""")
_WS_RE = re.compile(r"[ \t ]+")
_BLANKS_RE = re.compile(r"\n{3,}")
_MSGID_RE = re.compile(r"<[^<>\s]+>")
_BLOCK_TAGS = ("p", "div", "li", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "pre", "hr", "section", "article", "header", "footer")


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    payload: bytes


@dataclass
class ParsedLink:
    shown_text: str
    url: str


@dataclass
class ParsedMail:
    message_id: str
    thread_id: str
    in_reply_to: str | None
    references: list[str]
    from_name: str
    from_address: str
    reply_to: str | None
    to_addresses: str
    cc_addresses: str
    subject: str
    date: datetime | None
    body_text: str
    body_html: str | None
    headers: dict[str, object]
    attachments: list[ParsedAttachment] = field(default_factory=list)
    links: list[ParsedLink] = field(default_factory=list)


def _decode(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if "=?" in text:
        try:
            text = str(make_header(decode_header(text)))
        except Exception:  # noqa: BLE001 - malformed encoded-words are common in spam
            pass
    return text.replace("\r", " ").replace("\n", " ").strip()


def _header_all(msg: Message, name: str) -> list[str]:
    try:
        return [_decode(v) for v in (msg.get_all(name) or [])]
    except Exception:  # noqa: BLE001
        return []


def _header(msg: Message, name: str) -> str:
    values = _header_all(msg, name)
    return values[0] if values else ""


def _addresses(values: list[str]) -> list[tuple[str, str]]:
    try:
        return [(n.strip(), a.strip().lower()) for n, a in getaddresses(values) if a or n]
    except Exception:  # noqa: BLE001
        return []


def _msg_ids(value: str) -> list[str]:
    return _MSGID_RE.findall(value or "")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")
    for cell in soup.find_all(("td", "th")):
        cell.insert_after(" ")
    return normalize_text(soup.get_text(""))


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def _part_text(part: Message) -> str:
    try:
        if isinstance(part, EmailMessage):
            content = part.get_content()
            if isinstance(content, str):
                return content
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)


def _filename(part: Message) -> str:
    try:
        name = part.get_filename()
    except Exception:  # noqa: BLE001
        name = None
    return _decode(name) if name else ""


def _walk(msg: Message) -> tuple[list[str], list[str], list[ParsedAttachment]]:
    plains: list[str] = []
    htmls: list[str] = []
    attachments: list[ParsedAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "application/octet-stream").lower()
        disposition = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
        filename = _filename(part)
        is_attachment = disposition == "attachment" or (filename and ctype not in ("text/plain", "text/html"))
        if ctype == "message/rfc822" or is_attachment:
            payload = part.get_payload(decode=True)
            if payload is None and ctype == "message/rfc822":
                inner = part.get_payload()
                payload = inner[0].as_bytes() if isinstance(inner, list) and inner else b""
            attachments.append(ParsedAttachment(
                filename=filename or ("attached-message.eml" if ctype == "message/rfc822" else "unnamed"),
                content_type=ctype,
                payload=payload or b"",
            ))
            continue
        if ctype == "text/plain":
            plains.append(_part_text(part))
        elif ctype == "text/html":
            htmls.append(_part_text(part))
    return plains, htmls, attachments


def extract_links(body_text: str, body_html: str | None) -> list[ParsedLink]:
    links: list[ParsedLink] = []
    seen: set[tuple[str, str]] = set()
    if body_html:
        soup = BeautifulSoup(body_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            if not href or href.startswith(("mailto:", "tel:", "#", "cid:")):
                continue
            shown = normalize_text(a.get_text(" "))[:500]
            key = (shown, href)
            if key not in seen:
                seen.add(key)
                links.append(ParsedLink(shown_text=shown, url=href[:4000]))
    known = {link.url for link in links}
    for match in _URL_RE.finditer(body_text or ""):
        url = match.group(1).rstrip(".,;:!?")
        if url.lower().startswith("www."):
            url = "http://" + url
        if url not in known:
            known.add(url)
            links.append(ParsedLink(shown_text="", url=url[:4000]))
    return links[:500]


def synthetic_message_id(raw: bytes, internal_date: datetime | None) -> str:
    digest = hashlib.sha256()
    digest.update(raw[:65536])
    if internal_date:
        digest.update(internal_date.isoformat().encode())
    return f"<synthetic-{digest.hexdigest()[:40]}@mailcat.local>"


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_message(raw: bytes, internal_date: datetime | None = None) -> ParsedMail:
    try:
        msg: Message = email.message_from_bytes(raw, policy=policy.default)
        # Touch a header to surface policy errors early on badly broken mail.
        msg.get("Subject")
    except Exception:  # noqa: BLE001
        msg = email.message_from_bytes(raw, policy=policy.compat32)

    message_id = next(iter(_msg_ids(_header(msg, "Message-ID"))), "") or synthetic_message_id(
        raw, internal_date
    )
    in_reply_to = next(iter(_msg_ids(_header(msg, "In-Reply-To"))), None)
    references = _msg_ids(" ".join(_header_all(msg, "References")))
    thread_id = references[0] if references else (in_reply_to or message_id)

    from_list = _addresses(_header_all(msg, "From"))
    from_name, from_address = from_list[0] if from_list else ("", "")
    reply_to = ", ".join(a for _, a in _addresses(_header_all(msg, "Reply-To"))) or None
    to_addresses = ", ".join(a for _, a in _addresses(_header_all(msg, "To")))
    cc_addresses = ", ".join(a for _, a in _addresses(_header_all(msg, "Cc")))

    plains, htmls, attachments = _walk(msg)
    body_html = "\n".join(htmls) if htmls else None
    plain = normalize_text("\n\n".join(plains)) if plains else ""
    body_text = plain or (html_to_text(body_html) if body_html else "")

    headers: dict[str, object] = {}
    for name in KEPT_HEADERS:
        values = _header_all(msg, name)
        if not values:
            continue
        headers[name] = [v[:2000] for v in values] if name in _MULTI else values[0][:2000]

    return ParsedMail(
        message_id=message_id[:998],
        thread_id=thread_id[:998],
        in_reply_to=in_reply_to,
        references=references,
        from_name=from_name[:500],
        from_address=from_address[:320],
        reply_to=reply_to,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        subject=_header(msg, "Subject")[:2000],
        date=parse_date(_header(msg, "Date")),
        body_text=body_text,
        body_html=body_html,
        headers=headers,
        attachments=attachments,
        links=extract_links(body_text, body_html),
    )
