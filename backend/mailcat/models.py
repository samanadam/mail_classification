"""Database schema. Mirrors the "Data model" section of the project plan.

A mail is identified by (account, Message-ID). Its mailbox location is tracked separately by
folder + UIDVALIDITY + UID, so mail moved or deleted by hand in a mail app is noticed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _now() -> Any:
    return func.now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # gmail | icloud | yandex | outlook | custom
    provider: Mapped[str] = mapped_column(String(20))
    email: Mapped[str] = mapped_column(String(255))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=993)
    # ssl (implicit TLS, default) | starttls | plain (plain only for local test servers)
    security: Mapped[str] = mapped_column(String(10), default="ssl")
    username: Mapped[str] = mapped_column(String(255))
    # password | oauth2
    auth_method: Mapped[str] = mapped_column(String(20), default="password")
    # Fernet-encrypted JSON: {"password": ...} or {"client_id":..., "tenant":..., "refresh_token":...}
    credentials_enc: Mapped[bytes] = mapped_column(LargeBinary)

    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    writeback_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    writeback_dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    deletion_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    deletion_dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    backfill_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Parent folder for category folders, e.g. "Sorted" -> "Sorted/Finance".
    sorted_parent: Mapped[str] = mapped_column(String(100), default="Sorted")
    # {category_id: {"move_days": n, "mailbox_delete_days": n|null, "local_delete_days": n|null}}
    timer_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Optional explicit folder names {"inbox":..,"sent":..,"spam":..,"trash":..,"all":..}
    folder_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # Text shown to the model as the meaning of this category.
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    # "undecided" and "spam" have behaviour attached; the key survives renames.
    system_key: Mapped[str | None] = mapped_column(String(20), unique=True)
    move_days: Mapped[int] = mapped_column(Integer, default=1)
    mailbox_delete_days: Mapped[int | None] = mapped_column(Integer)
    local_delete_days: Mapped[int | None] = mapped_column(Integer)
    writeback_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # False: the model never picks this category; only rules assign it.
    model_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    color: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class FolderState(Base):
    __tablename__ = "folder_state"
    __table_args__ = (UniqueConstraint("account_id", "folder"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    folder: Mapped[str] = mapped_column(String(255))
    # inbox | sent | spam | sorted
    role: Mapped[str] = mapped_column(String(20))
    # For sorted folders: the category the folder belongs to (survives renames).
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    high_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    # Backfill walks downwards from here, newest first. None = not started.
    backfill_low_uid: Mapped[int | None] = mapped_column(BigInteger)
    backfill_done: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Mail(Base):
    __tablename__ = "mails"
    __table_args__ = (
        UniqueConstraint("account_id", "message_id"),
        Index("ix_mails_received", "received_at"),
        Index("ix_mails_category", "category_id"),
        Index("ix_mails_thread", "account_id", "thread_id"),
        Index("ix_mails_location", "account_id", "folder", "uid"),
        Index("ix_mails_search", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(String(998))
    thread_id: Mapped[str] = mapped_column(String(998))
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    references_header: Mapped[str | None] = mapped_column(Text)

    # inbox | sent | spam | sorted | missing
    folder_role: Mapped[str] = mapped_column(String(20))
    folder: Mapped[str | None] = mapped_column(String(255))
    uid: Mapped[int | None] = mapped_column(BigInteger)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    from_name: Mapped[str] = mapped_column(Text, default="")
    from_address: Mapped[str] = mapped_column(String(320), default="")
    reply_to: Mapped[str | None] = mapped_column(Text)
    to_addresses: Mapped[str] = mapped_column(Text, default="")
    cc_addresses: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    size: Mapped[int] = mapped_column(Integer, default=0)
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str | None] = mapped_column(Text)
    # Headers relevant to classification and security checks.
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    seen: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)

    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    confidence: Mapped[float | None] = mapped_column(Float)
    # rule | model | thread | security | correction | fallback
    decided_by: Mapped[str | None] = mapped_column(String(20))
    decision_detail: Mapped[str | None] = mapped_column(Text)
    probabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classifier_version: Mapped[str | None] = mapped_column(String(100))
    # Set by a correction; re-classification never overrides it.
    category_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    malicious: Mapped[bool] = mapped_column(Boolean, default=False)
    malicious_reasons: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    phishing_score: Mapped[float | None] = mapped_column(Float)
    needs_action: Mapped[bool] = mapped_column(Boolean, default=False)
    security_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Rescued from deletion in the dashboard: never deleted by retention.
    keep: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set by undo: write-back leaves this mail where it is until its category is changed.
    hold_writeback: Mapped[bool] = mapped_column(Boolean, default=False)
    moved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mailbox_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_now(), onupdate=func.now()
    )

    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', coalesce(subject, '')), 'A') || "
            "setweight(to_tsvector('simple', coalesce(from_name, '') || ' ' || coalesce(from_address, '')), 'B') || "
            "setweight(to_tsvector('simple', left(coalesce(body_text, ''), 200000)), 'C')",
            persisted=True,
        ),
    )

    account: Mapped[Account] = relationship()
    category: Mapped[Category | None] = relationship()
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="mail", cascade="all, delete-orphan", passive_deletes=True
    )
    links: Mapped[list[Link]] = relationship(
        back_populates="mail", cascade="all, delete-orphan", passive_deletes=True
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    mail_id: Mapped[int] = mapped_column(ForeignKey("mails.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(Text, default="")
    # Random name on the noexec attachment volume; never derived from the original name.
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    content_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    # pending | clean | infected | error | skipped
    scan_status: Mapped[str] = mapped_column(String(20), default="pending")
    scan_detail: Mapped[str | None] = mapped_column(Text)
    risky: Mapped[bool] = mapped_column(Boolean, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    mail: Mapped[Mail] = relationship(back_populates="attachments")


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(primary_key=True)
    mail_id: Mapped[int] = mapped_column(ForeignKey("mails.id", ondelete="CASCADE"), index=True)
    shown_text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(String(255), default="")
    # ok | suspicious | malicious
    verdict: Mapped[str] = mapped_column(String(20), default="ok")
    reasons: Mapped[list[Any]] = mapped_column(JSONB, default=list)

    mail: Mapped[Mail] = relationship(back_populates="links")


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # [{"field": "from_domain", "op": "equals", "value": "github.com"}, ...]; all must match.
    conditions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # assign: set category directly. restrict: let the model choose among candidate categories.
    action: Mapped[str] = mapped_column(String(20), default="assign")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    candidate_category_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class SenderTimer(Base):
    """Delete timers for one sender address or a whole domain ("@shop.com"). They take precedence
    over category and account timers; protected mail is still never deleted."""

    __tablename__ = "sender_timers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "news@shop.com" or "@shop.com" (also matches subdomains), stored lower-case
    pattern: Mapped[str] = mapped_column(String(320), unique=True)
    mailbox_delete_days: Mapped[int | None] = mapped_column(Integer)
    local_delete_days: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    mail_id: Mapped[int | None] = mapped_column(ForeignKey("mails.id", ondelete="SET NULL"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    message_id: Mapped[str] = mapped_column(String(998))
    old_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    new_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    old_decided_by: Mapped[str | None] = mapped_column(String(20))
    old_confidence: Mapped[float | None] = mapped_column(Float)
    # dashboard | mail_app
    source: Mapped[str] = mapped_column(String(20))
    # Model input snapshot (sender, subject, trimmed body) for evaluation and fine-tuning.
    # Cleared when the mail's local copy is deleted.
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class ActionLog(Base):
    __tablename__ = "actions_log"
    __table_args__ = (Index("ix_actions_run", "run_id"), Index("ix_actions_created", "created_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    mail_id: Mapped[int | None] = mapped_column(ForeignKey("mails.id", ondelete="SET NULL"))
    message_id: Mapped[str | None] = mapped_column(String(998))
    # move | delete | local_delete | undo_move | undo_delete | folder_create | folder_rename
    action: Mapped[str] = mapped_column(String(30))
    from_folder: Mapped[str | None] = mapped_column(String(255))
    to_folder: Mapped[str | None] = mapped_column(String(255))
    rule: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    mail_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class Tombstone(Base):
    """A locally deleted mail. No content, only what is needed to never download it again and
    to still apply a later mailbox timer."""

    __tablename__ = "tombstones"
    __table_args__ = (UniqueConstraint("account_id", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(String(998))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    folder_role: Mapped[str | None] = mapped_column(String(20))
    folder: Mapped[str | None] = mapped_column(String(255))
    uid: Mapped[int | None] = mapped_column(BigInteger)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    keep: Mapped[bool] = mapped_column(Boolean, default=False)
    mailbox_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_now(), onupdate=func.now()
    )


class SettingsLog(Base):
    __tablename__ = "settings_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # e.g. "confidence_threshold", "category:4.mailbox_delete_days", "account:2.deletion_enabled"
    key: Mapped[str] = mapped_column(String(200))
    old_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    changed_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # scheduled | manual | undo | cli
    kind: Mapped[str] = mapped_column(String(20))
    triggered_by: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # running | ok | partial | failed | aborted
    status: Mapped[str] = mapped_column(String(20), default="running")
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    errors: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunRequest(Base):
    """'Sync now' queue. The scheduler picks pending requests up within seconds."""

    __tablename__ = "run_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str | None] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    picked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
