"""Corrections: a category change made by the user, in the dashboard or by dragging a mail into
another category folder in a mail app. They form the test set and later the fine-tuning data."""
from __future__ import annotations

from sqlalchemy.orm import Session

from mailcat.models import Correction, Mail


def record_correction(session: Session, mail: Mail, new_category_id: int, source: str,
                       settings: dict) -> None:
    session.add(Correction(
        mail_id=mail.id, account_id=mail.account_id, message_id=mail.message_id,
        old_category_id=mail.category_id, new_category_id=new_category_id,
        old_decided_by=mail.decided_by, old_confidence=mail.confidence, source=source,
        features=correction_features(mail, int(settings["body_max_chars"])),
    ))
    mail.category_id = new_category_id
    mail.decided_by = "correction"
    mail.decision_detail = f"corrected in {'a mail app' if source == 'mail_app' else 'the dashboard'}"
    mail.confidence = 1.0
    mail.category_locked = True


def correction_features(mail: Mail, max_chars: int) -> dict:
    return {"sender": f"{mail.from_name} <{mail.from_address}>".strip(), "subject": mail.subject,
            "body": (mail.body_text or "")[:max_chars], "headers": {
                k: v for k, v in (mail.headers or {}).items() if k in ("list-unsubscribe", "list-id")}}
