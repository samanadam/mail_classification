from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.api.deps import current_user, get_db
from mailcat.crypto import decrypt_credentials, encrypt_credentials
from mailcat.imap.providers import PROVIDERS
from mailcat.models import Account, Category, Mail, Run, SettingsLog, User
from mailcat.worker.accounts import open_session
from mailcat.worker.policy import TIMER_KEYS

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

SWITCHES = ("sync_enabled", "writeback_enabled", "writeback_dry_run", "deletion_enabled",
            "deletion_dry_run", "backfill_enabled")
Provider = Literal["gmail", "icloud", "yandex", "outlook", "custom"]
Security = Literal["ssl", "starttls", "plain"]


def _out(a: Account, mail_count: int = 0) -> dict[str, Any]:
    return {
        "id": a.id, "name": a.name, "provider": a.provider, "email": a.email, "host": a.host, "port": a.port,
        "security": a.security, "username": a.username, "auth_method": a.auth_method,
        **{s: getattr(a, s) for s in SWITCHES},
        "sorted_parent": a.sorted_parent, "timer_overrides": a.timer_overrides or {},
        "folder_overrides": a.folder_overrides or {}, "last_sync_at": a.last_sync_at,
        "last_sync_ok": a.last_sync_ok, "last_sync_error": a.last_sync_error,
        "consecutive_failures": a.consecutive_failures, "mail_count": mail_count,
    }


@router.get("/providers")
def providers(_: User = Depends(current_user)) -> list[dict]:
    return [{"key": p.key, "label": p.label, "host": p.host, "port": p.port, "auth_method": p.auth_method,
             "notes": p.notes} for p in PROVIDERS.values()]


@router.get("")
def list_accounts(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[dict]:
    counts = dict(db.execute(select(Mail.account_id, func.count()).group_by(Mail.account_id)).all())
    query = select(Account).where(Account.sandbox_root.is_(None)).order_by(Account.id)
    return [_out(a, counts.get(a.id, 0)) for a in db.scalars(query)]


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Provider
    email: str = Field(min_length=3, max_length=255)
    host: str | None = Field(default=None, max_length=255)
    port: int = Field(default=993, ge=1, le=65535)
    security: Security = "ssl"
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=1000)
    oauth_client_id: str | None = Field(default=None, max_length=200)
    oauth_tenant: str | None = Field(default=None, max_length=200)
    sorted_parent: str = Field(default="Sorted", min_length=1, max_length=100)


@router.post("")
def create_account(body: AccountIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    provider = PROVIDERS[body.provider]
    host = body.host or provider.host
    if not host:
        raise HTTPException(400, "host is required for a custom IMAP account")
    if body.security == "plain" and host not in ("localhost", "127.0.0.1"):
        raise HTTPException(400, "plain IMAP is only allowed for a local test server")
    if db.scalar(select(Account).where(Account.name == body.name)):
        raise HTTPException(409, "an account with this name exists")
    if provider.auth_method == "oauth2":
        if not body.oauth_client_id:
            raise HTTPException(400, "Outlook needs the Azure app's client id")
        creds = {"client_id": body.oauth_client_id, "tenant": body.oauth_tenant or "common"}
        auth = "oauth2"
    else:
        if not body.password:
            raise HTTPException(400, "password (app password) is required")
        creds = {"password": body.password}
        auth = "password"
    account = Account(
        name=body.name, provider=body.provider, email=body.email.strip(), host=host, port=body.port,
        security=body.security, username=(body.username or body.email).strip(), auth_method=auth,
        credentials_enc=encrypt_credentials(creds), sorted_parent=body.sorted_parent.strip(),
        timer_overrides={}, folder_overrides={},
        # Safe by default: read-only until a dry-run period has been reviewed.
        sync_enabled=True, writeback_enabled=False, writeback_dry_run=True, deletion_enabled=False,
        deletion_dry_run=True, backfill_enabled=False,
    )
    db.add(account)
    db.flush()
    settings_store.log_change(db, f"account:{account.id}", None, {"created": account.name}, user.username)
    return _out(account)


class AccountPatch(BaseModel):
    sync_enabled: bool | None = None
    writeback_enabled: bool | None = None
    writeback_dry_run: bool | None = None
    deletion_enabled: bool | None = None
    deletion_dry_run: bool | None = None
    backfill_enabled: bool | None = None
    password: str | None = Field(default=None, max_length=1000)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    sorted_parent: str | None = Field(default=None, min_length=1, max_length=100)
    timer_overrides: dict[str, dict[str, int | None]] | None = None
    folder_overrides: dict[str, str] | None = None


def dry_run_completed(db: Session, account: Account, switch: str) -> bool:
    """True when the feature has been on in dry-run mode during at least one finished run."""
    enabled_at = db.scalar(select(func.max(SettingsLog.created_at)).where(
        SettingsLog.key == f"account:{account.id}.{switch}",
        SettingsLog.new_value == cast(literal("true"), JSONB)))
    if enabled_at is None:
        return False
    return db.scalar(select(func.count()).select_from(Run).where(
        Run.kind.in_(("scheduled", "manual", "cli")), Run.started_at > enabled_at,
        Run.status.in_(("ok", "partial")))) > 0


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountPatch, db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    changes = body.model_dump(exclude_unset=True)
    # Going live requires a dry-run period first (plan: "Safe by default").
    for feature, dry in (("writeback_enabled", "writeback_dry_run"), ("deletion_enabled", "deletion_dry_run")):
        if changes.get(dry) is False and getattr(account, dry):
            enabled = changes.get(feature, getattr(account, feature))
            if enabled and not dry_run_completed(db, account, feature):
                raise HTTPException(409, f"turn on {feature.split('_')[0]} in dry-run mode and review at least "
                                         "one run's planned actions before going live")
    if "timer_overrides" in changes and changes["timer_overrides"] is not None:
        valid_ids = {str(c) for c in db.scalars(select(Category.id))}
        for cid, values in changes["timer_overrides"].items():
            if cid not in valid_ids or not set(values) <= set(TIMER_KEYS):
                raise HTTPException(400, f"invalid timer override for category {cid}")
            for v in values.values():
                if v is not None and not (0 <= v <= 36500):
                    raise HTTPException(400, "timer values must be between 0 and 36500 days")
    if "password" in changes:
        password = changes.pop("password")
        if password:
            creds = decrypt_credentials(account.credentials_enc)
            creds["password"] = password
            account.credentials_enc = encrypt_credentials(creds)
            settings_store.log_change(db, f"account:{account.id}.password", None, "changed", user.username)
    for key, value in changes.items():
        if value is None and key not in ("timer_overrides", "folder_overrides"):
            continue
        old = getattr(account, key)
        if old != value:
            setattr(account, key, value)
            settings_store.log_change(db, f"account:{account.id}.{key}", old, value, user.username)
    return _out(account)


@router.delete("/{account_id}")
def delete_account(account_id: int, confirm: str = "", db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    if confirm != account.name:
        raise HTTPException(400, "pass confirm=<account name>; this removes every local copy of its mail "
                                 "(the mailbox itself is not touched)")
    settings_store.log_change(db, f"account:{account.id}", {"name": account.name}, None, user.username)
    db.delete(account)
    return {"ok": True}


@router.post("/{account_id}/test")
def test_account(account_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    try:
        with open_session(db, account) as imap:
            roles = {role: imap.resolve_role(role) for role in ("inbox", "sent", "spam", "trash", "all")}
            caps = sorted(imap.capabilities)
            folders = [f.name for f in imap.folders()]
            inbox_uidvalidity, inbox_count = imap.select("INBOX", readonly=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    warnings = []
    if "MOVE" not in caps and "UIDPLUS" not in caps:
        warnings.append("server supports neither MOVE nor UIDPLUS; moves may fail safely")
    if roles["trash"] is None:
        warnings.append("no Trash folder found; mailbox deletion will not run (set a folder override)")
    if account.provider == "gmail" and roles["all"] is None:
        warnings.append("Gmail All Mail not found; deletes cannot be verified")
    return {"ok": True, "roles": roles, "capabilities": caps, "folder_count": len(folders),
            "folders": folders[:200], "inbox_messages": inbox_count, "warnings": warnings}
