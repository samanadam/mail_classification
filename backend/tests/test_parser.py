from datetime import UTC, datetime
from email.message import EmailMessage

from mailcat.imap.client import parse_fetch, parse_internaldate
from mailcat.ingest.attachments import is_risky
from mailcat.ingest.parser import html_to_text, parse_message


def _multipart() -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = "<abc@example.org>"
    msg["From"] = "=?utf-8?b?w4dpw6dlayBCYW5rYXPEsQ==?= <Ekstre@Bank.com.tr>"
    msg["To"] = "me@example.org, You <you@example.org>"
    msg["Subject"] = "=?utf-8?q?Hesap_=C3=B6zeti?="
    msg["References"] = "<root@x> <mid@x>"
    msg["In-Reply-To"] = "<mid@x>"
    msg["List-Unsubscribe"] = "<https://example.org/u>"
    msg["Authentication-Results"] = "mx.google.com; dkim=pass; spf=pass"
    msg["Date"] = "Tue, 01 Sep 2026 10:00:00 +0300"
    msg.set_content("Plain body with https://example.org/a link.")
    msg.add_alternative('<p>Hi <a href="https://evil.example.com/x">https://bank.com.tr</a></p>', subtype="html")
    msg.add_attachment(b"MZ\x90\x00binary", maintype="application", subtype="octet-stream",
                       filename="invoice.pdf.exe")
    return msg.as_bytes()


def test_parse_multipart_message():
    parsed = parse_message(_multipart())
    assert parsed.message_id == "<abc@example.org>"
    assert parsed.thread_id == "<root@x>"
    assert parsed.in_reply_to == "<mid@x>"
    assert parsed.from_address == "ekstre@bank.com.tr"
    assert parsed.from_name == "Çiçek Bankası"
    assert parsed.subject == "Hesap özeti"
    assert parsed.to_addresses == "me@example.org, you@example.org"
    assert "Plain body" in parsed.body_text
    assert parsed.body_html and "<a href" in parsed.body_html
    assert parsed.headers["list-unsubscribe"] == "<https://example.org/u>"
    assert parsed.headers["authentication-results"] == ["mx.google.com; dkim=pass; spf=pass"]
    assert parsed.date == datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
    [att] = parsed.attachments
    assert att.filename == "invoice.pdf.exe" and att.payload.startswith(b"MZ")
    urls = {(link.shown_text, link.url) for link in parsed.links}
    assert ("https://bank.com.tr", "https://evil.example.com/x") in urls
    assert ("", "https://example.org/a") in urls


def test_missing_message_id_gets_stable_synthetic_id():
    raw = b"From: a@b.c\r\nSubject: x\r\n\r\nbody"
    first, second = parse_message(raw), parse_message(raw)
    assert first.message_id.startswith("<synthetic-") and first.message_id == second.message_id
    assert first.thread_id == first.message_id


def test_html_only_mail_gets_text_body():
    raw = (b"Message-ID: <h@x>\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b"<html><style>p{}</style><script>alert(1)</script><p>Merhaba<br>d\xc3\xbcnya</p></html>")
    parsed = parse_message(raw)
    assert parsed.body_text == "Merhaba\ndünya"
    assert html_to_text("<p>a</p><p>b <b>bold</b> c</p>") == "a\n\nb bold c"


def test_broken_mail_does_not_crash():
    parsed = parse_message(b"\x00\xff garbage without headers")
    assert parsed.message_id.startswith("<synthetic-")


def test_risky_attachment_detection():
    assert is_risky("invoice.pdf.exe", "application/pdf", b"%PDF")
    assert is_risky("report.xlsm", "application/octet-stream", b"")
    assert is_risky("photo.jpg", "image/jpeg", b"MZ\x90")  # executable disguised by name
    assert is_risky("archive.zip", "application/zip", b"PK\x03\x04")
    assert not is_risky("statement.pdf", "application/pdf", b"%PDF-1.7")


def test_parse_fetch_handles_flags_after_literal():
    data = [
        (b'1 (UID 11 FLAGS (\\Seen) INTERNALDATE "01-Sep-2026 10:00:00 +0300" RFC822.SIZE 4 BODY[] {4}', b"abcd"),
        b")",
        (b'2 (UID 12 INTERNALDATE " 2-Sep-2026 10:00:00 +0000" RFC822.SIZE 3 BODY[] {3}', b"xyz"),
        b" FLAGS (\\Flagged \\Seen))",
    ]
    first, second = parse_fetch(data)
    assert (first.uid, first.flags, first.raw) == (11, ("\\Seen",), b"abcd")
    assert first.internal_date == datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
    assert second.uid == 12 and second.flagged and second.seen and second.raw == b"xyz"
    flags_only = parse_fetch([b"1 (UID 5 FLAGS ())", b"2 (UID 9 FLAGS (\\Flagged))"])
    assert [(r.uid, r.flagged) for r in flags_only] == [(5, False), (9, True)]
    assert parse_internaldate("bad") is None
