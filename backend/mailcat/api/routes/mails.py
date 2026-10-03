from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from mailcat import settings_store
from mailcat.api.deps import current_user, get_db
from mailcat.api.render import safe_html
from mailcat.corrections import record_correction
from mailcat.ingest import attachments as attachment_store
from mailcat.models import Account, Attachment, Category, Mail, User

router = APIRouter(prefix="/api/mails", tags=["mails"])

LOW_CONFIDENCE_MARGIN = 0.15
_SAFE_NAME = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)


class MailSummary(BaseModel):
    id: int
    account_id: int
    account_name: str
    from_name: str
    from_address: str
    subject: str
    snippet: str
    received_at: datetime
    seen: bool
    flagged: bool
    category_id: int | None
    confidence: float | None
    decided_by: str | None
    malicious: bool
    needs_action: bool
    has_attachments: bool
    folder_role: str
    folder: str | None
    keep: bool


class MailPage(BaseModel):
    items: list[MailSummary]
    total: int
    page: int
    page_size: int


def _snippet(text: str) -> str:
    return re.sub(r"\s+", " ", text or "")[:180]


@router.get("", response_model=MailPage)
def list_mails(
    db: Session = Depends(get_db), _: User = Depends(current_user),
    category_id: int | None = None, uncategorized: bool = False, account_id: int | None = None,
    seen: bool | None = None, flagged: bool | None = None, malicious: bool | None = None,
    needs_action: bool | None = None, has_attachments: bool | None = None,
    role: Literal["inbox", "sorted", "spam", "sent", "missing", "trash"] | None = None,
    review: bool = False, misfiled: bool = False, q: str | None = Query(None, max_length=300),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
) -> MailPage:
    conds: list[Any] = []
    if category_id is not None:
        conds.append(Mail.category_id == category_id)
    if uncategorized:
        conds.append(Mail.category_id.is_(None))
    if account_id is not None:
        conds.append(Mail.account_id == account_id)
    if seen is not None:
        conds.append(Mail.seen.is_(seen))
    if flagged is not None:
        conds.append(Mail.flagged.is_(flagged))
    if malicious is not None:
        conds.append(Mail.malicious.is_(malicious))
    if needs_action is not None:
        conds.append(Mail.needs_action.is_(needs_action))
    att_exists = exists().where(Attachment.mail_id == Mail.id)
    if has_attachments is not None:
        conds.append(att_exists if has_attachments else ~att_exists)
    if role is not None:
        conds.append(Mail.folder_role == role)
    if review:
        threshold = float(settings_store.get(db, "confidence_threshold"))
        undecided = select(Category.id).where(Category.system_key == "undecided").scalar_subquery()
        conds.append(Mail.category_locked.is_(False))
        conds.append(or_(
            Mail.category_id == undecided,
            and_(Mail.decided_by.in_(("model", "thread")),
                 Mail.confidence < threshold + LOW_CONFIDENCE_MARGIN),
        ))
    if misfiled:
        spam = select(Category.id).where(Category.system_key == "spam").scalar_subquery()
        conds.append(Mail.folder_role == "spam")
        conds.append(Mail.category_id != spam)
    if q and q.strip():
        conds.append(Mail.search_vector.op("@@")(func.websearch_to_tsquery("simple", q.strip())))

    base = select(Mail).where(*conds)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        select(Mail, Account.name, att_exists.label("has_att"))
        .join(Account, Account.id == Mail.account_id)
        .where(*conds)
        .order_by(Mail.received_at.desc(), Mail.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    items = [MailSummary(
        id=m.id, account_id=m.account_id, account_name=name, from_name=m.from_name,
        from_address=m.from_address, subject=m.subject, snippet=_snippet(m.body_text),
        received_at=m.received_at, seen=m.seen, flagged=m.flagged, category_id=m.category_id,
        confidence=m.confidence, decided_by=m.decided_by, malicious=m.malicious,
        needs_action=m.needs_action, has_attachments=bool(has_att), folder_role=m.folder_role,
        folder=m.folder, keep=m.keep,
    ) for m, name, has_att in rows]
    return MailPage(items=items, total=total, page=page, page_size=page_size)


def _get_mail(db: Session, mail_id: int) -> Mail:
    mail = db.scalar(select(Mail).where(Mail.id == mail_id).options(
        selectinload(Mail.attachments), selectinload(Mail.links)))
    if mail is None:
        raise HTTPException(404, "mail not found")
    return mail


@router.get("/{mail_id}")
def get_mail(mail_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict[str, Any]:
    mail = _get_mail(db, mail_id)
    account = db.get(Account, mail.account_id)
    headers = mail.headers or {}
    from mailcat.security.headers import check_headers

    hv = check_headers(headers, mail.from_name, mail.from_address)
    return {
        "id": mail.id, "account_id": mail.account_id, "account_name": account.name if account else "",
        "message_id": mail.message_id, "thread_id": mail.thread_id,
        "from_name": mail.from_name, "from_address": mail.from_address, "reply_to": mail.reply_to,
        "to": mail.to_addresses, "cc": mail.cc_addresses, "subject": mail.subject,
        "received_at": mail.received_at, "size": mail.size, "body_text": mail.body_text,
        "has_html": bool(mail.body_html), "seen": mail.seen, "flagged": mail.flagged,
        "folder_role": mail.folder_role, "folder": mail.folder,
        "category_id": mail.category_id, "confidence": mail.confidence, "decided_by": mail.decided_by,
        "decision_detail": mail.decision_detail, "probabilities": mail.probabilities,
        "category_locked": mail.category_locked, "classifier_version": mail.classifier_version,
        "malicious": mail.malicious, "malicious_reasons": mail.malicious_reasons,
        "phishing_score": mail.phishing_score, "needs_action": mail.needs_action,
        "keep": mail.keep, "hold_writeback": mail.hold_writeback, "moved_at": mail.moved_at,
        "mailbox_deleted_at": mail.mailbox_deleted_at,
        "auth": {"spf": hv.spf, "dkim": hv.dkim, "dmarc": hv.dmarc, "warnings": hv.hard + hv.soft},
        "list_unsubscribe": bool(headers.get("list-unsubscribe")),
        "links": [{"id": link.id, "shown_text": link.shown_text, "url": link.url, "host": link.host,
                   "verdict": link.verdict, "reasons": link.reasons} for link in mail.links],
        "attachments": [{
            "id": a.id, "filename": a.filename, "content_type": a.content_type, "size": a.size,
            "scan_status": a.scan_status, "scan_detail": a.scan_detail, "risky": a.risky,
            "locked": a.locked or mail.malicious, "sha256": a.sha256,
        } for a in mail.attachments],
        "thread": [{
            "id": t.id, "subject": t.subject, "from_address": t.from_address, "received_at": t.received_at,
            "folder_role": t.folder_role,
        } for t in db.scalars(select(Mail).where(
            Mail.account_id == mail.account_id, Mail.thread_id == mail.thread_id, Mail.id != mail.id,
        ).order_by(Mail.received_at).limit(50))],
    }


@router.get("/{mail_id}/html")
def get_mail_html(mail_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    if not settings_store.get(db, "html_view_enabled"):
        raise HTTPException(403, "HTML view is disabled in settings")
    mail = db.get(Mail, mail_id)
    if mail is None:
        raise HTTPException(404, "mail not found")
    if not mail.body_html:
        raise HTTPException(404, "this mail has no HTML part")
    html, blocked = safe_html(mail.body_html)
    return {"html": html, "blocked_images": blocked}


class MailUpdate(BaseModel):
    category_id: int | None = None
    keep: bool | None = None
    hold_writeback: bool | None = None


@router.patch("/{mail_id}")
def update_mail(mail_id: int, body: MailUpdate, db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> dict:
    mail = db.get(Mail, mail_id)
    if mail is None:
        raise HTTPException(404, "mail not found")
    if body.category_id is not None and body.category_id != mail.category_id:
        if db.get(Category, body.category_id) is None:
            raise HTTPException(400, "unknown category")
        if mail.folder_role == "sent":
            raise HTTPException(400, "sent mail is never categorized")
        settings = settings_store.get_all(db)
        record_correction(db, mail, body.category_id, "dashboard", settings)
        mail.hold_writeback = False
    if body.keep is not None:
        mail.keep = body.keep
    if body.hold_writeback is not None:
        mail.hold_writeback = body.hold_writeback
    return {"ok": True, "category_id": mail.category_id, "keep": mail.keep,
            "hold_writeback": mail.hold_writeback, "by": user.username}


@router.get("/attachments/{attachment_id}/download")
def download_attachment(attachment_id: int, confirm_risky: bool = False, db: Session = Depends(get_db),
                        _: User = Depends(current_user)) -> FileResponse:
    att = db.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(404, "attachment not found")
    mail = db.get(Mail, att.mail_id)
    if mail is None or mail.malicious:
        raise HTTPException(403, "downloads are blocked for mail flagged malicious")
    if att.locked or att.scan_status == "infected":
        raise HTTPException(403, "this attachment is locked: the virus scanner flagged it")
    if att.risky and not confirm_risky:
        raise HTTPException(409, "risky file type; confirm to download")
    if att.scan_status != "clean" and not confirm_risky:
        raise HTTPException(409, "not scanned by the virus scanner; confirm to download")
    path = attachment_store.path_for(att.stored_name)
    if not path.exists():
        raise HTTPException(410, "attachment file is gone")
    name = _SAFE_NAME.sub("_", att.filename or "attachment").strip(". ") or "attachment"
    return FileResponse(path, media_type="application/octet-stream", filename=name[:150],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
