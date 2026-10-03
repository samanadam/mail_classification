"""Timers and protections shared by the worker, the deletion preview and the settings impact check,
so the dashboard always shows exactly what the next run will do."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from mailcat.models import Account, Category, Mail, SenderTimer, Tombstone

TIMER_KEYS = ("move_days", "mailbox_delete_days", "local_delete_days")
MANAGED_ROLES = ("inbox", "sorted")


def effective_timer(account: Account, category: Category, key: str) -> int | None:
    override = (account.timer_overrides or {}).get(str(category.id), {})
    if key in override:
        value = override[key]
        return None if value is None else int(value)
    return getattr(category, key)


def mode(enabled: bool, dry_run: bool) -> str:
    return "off" if not enabled else ("dry_run" if dry_run else "live")


def protection_reason(item: Mail | Tombstone, category: Category | None, settings: dict[str, Any]) -> str | None:
    """Why this mail must not be deleted, or None."""
    if item.keep:
        return "rescued"
    if settings.get("protect_flagged", True) and item.flagged:
        return "starred/flagged"
    if settings.get("protect_undecided", True) and (category is None or category.system_key == "undecided"):
        return "undecided"
    if isinstance(item, Mail):
        if settings.get("protect_needs_action") and item.needs_action:
            return "needs action"
        if settings.get("protect_unread") and not item.seen:
            return "unread"
        protected = settings.get("protected_senders") or []
        sender = (item.from_address or "").lower()
        domain = sender.rsplit("@", 1)[-1]
        for entry in protected:
            entry = entry.lower()
            if entry == sender or (entry.startswith("@") and (domain == entry[1:] or domain.endswith("." + entry[1:]))):
                return "protected sender"
    return None


@dataclass
class PlannedDeletion:
    kind: str  # mailbox | local
    account: Account
    category: Category
    days: int
    mail: Mail | None = None
    tombstone: Tombstone | None = None
    mode: str = "live"
    # Set when a sender timer, not the category, triggered the deletion.
    sender_pattern: str | None = None

    @property
    def message_id(self) -> str:
        return self.mail.message_id if self.mail else self.tombstone.message_id  # type: ignore[union-attr]

    @property
    def received_at(self) -> datetime | None:
        return self.mail.received_at if self.mail else self.tombstone.received_at  # type: ignore[union-attr]

    @property
    def rule(self) -> str:
        where = "mailbox" if self.kind == "mailbox" else "local copy"
        source = f"sender {self.sender_pattern}" if self.sender_pattern else self.category.name
        return f"{source}: delete {where} after {self.days} days"


def normalize_sender_pattern(pattern: str) -> str:
    """'News@Shop.com' -> 'news@shop.com'; 'shop.com' or '@Shop.com' -> '@shop.com'."""
    value = pattern.strip().lower()
    if not value:
        raise ValueError("sender pattern is empty")
    if "@" not in value:
        value = "@" + value
    local, _, domain = value.partition("@")
    if not domain or "." not in domain or " " in value or value.count("@") > 1:
        raise ValueError("use an address like news@shop.com or a domain like @shop.com")
    return f"{local}@{domain}" if local else f"@{domain}"


def sender_matches(pattern: str, address: str) -> bool:
    address = (address or "").lower()
    if not pattern.startswith("@"):
        return address == pattern
    domain = address.rsplit("@", 1)[-1]
    return domain == pattern[1:] or domain.endswith("." + pattern[1:])


def _specificity(timer: SenderTimer) -> tuple[int, int]:
    # exact addresses first, then longer (more specific) domains
    return (0 if not timer.pattern.startswith("@") else 1, -len(timer.pattern))


def sender_timer_for(timers: list[SenderTimer], address: str) -> SenderTimer | None:
    for timer in timers:
        if sender_matches(timer.pattern, address):
            return timer
    return None


def plan_deletions(
    session: Session, settings: dict[str, Any], now: datetime,
    category_overrides: dict[int, dict[str, int | None]] | None = None,
    account_ids: list[int] | None = None,
    sender_timers: list[SenderTimer] | None = None,
) -> list[PlannedDeletion]:
    """Everything whose delete timer has expired, for every account with sync on.
    `category_overrides` and `sender_timers` let the dashboard ask "what if" before saving."""
    category_overrides = category_overrides or {}
    if sender_timers is None:
        sender_timers = list(session.scalars(select(SenderTimer).where(SenderTimer.enabled.is_(True))))
    senders = sorted((t for t in sender_timers if t.enabled), key=_specificity)
    categories = {c.id: c for c in session.scalars(select(Category))}
    accounts = list(session.scalars(select(Account).where(Account.sync_enabled.is_(True))))
    if account_ids is not None:
        accounts = [a for a in accounts if a.id in account_ids]
    plans: list[PlannedDeletion] = []

    def timer(account: Account, category: Category, key: str) -> int | None:
        if category.id in category_overrides and key in category_overrides[category.id]:
            return category_overrides[category.id][key]
        return effective_timer(account, category, key)

    for account in accounts:
        account_mode = mode(account.deletion_enabled, account.deletion_dry_run)
        for category in categories.values():
            mbox_days = timer(account, category, "mailbox_delete_days")
            local_days = timer(account, category, "local_delete_days")
            if mbox_days is not None:
                cutoff = now - timedelta(days=mbox_days)
                mails = session.scalars(select(Mail).where(
                    Mail.account_id == account.id, Mail.category_id == category.id,
                    Mail.folder_role.in_(MANAGED_ROLES), Mail.mailbox_deleted_at.is_(None),
                    Mail.received_at <= cutoff))
                for mail in mails:
                    if _sender_decides(senders, mail, "mailbox_delete_days"):
                        continue
                    if protection_reason(mail, category, settings) is None:
                        plans.append(PlannedDeletion("mailbox", account, category, mbox_days, mail=mail,
                                                     mode=account_mode))
                tombs = session.scalars(select(Tombstone).where(
                    Tombstone.account_id == account.id, Tombstone.category_id == category.id,
                    Tombstone.folder_role.in_(MANAGED_ROLES), Tombstone.mailbox_deleted_at.is_(None),
                    Tombstone.received_at <= cutoff))
                for tomb in tombs:
                    if protection_reason(tomb, category, settings) is None:
                        plans.append(PlannedDeletion("mailbox", account, category, mbox_days, tombstone=tomb,
                                                     mode=account_mode))
            if local_days is not None:
                cutoff = now - timedelta(days=local_days)
                mails = session.scalars(select(Mail).where(
                    Mail.account_id == account.id, Mail.category_id == category.id,
                    Mail.received_at <= cutoff))
                for mail in mails:
                    if _sender_decides(senders, mail, "local_delete_days"):
                        continue
                    if protection_reason(mail, category, settings) is None:
                        plans.append(PlannedDeletion("local", account, category, local_days, mail=mail,
                                                     mode=account_mode))
        plans.extend(_sender_plans(session, settings, now, account, account_mode, senders, categories))
    return plans


def _sender_decides(senders: list[SenderTimer], mail: Mail, key: str) -> bool:
    timer = sender_timer_for(senders, mail.from_address)
    return timer is not None and getattr(timer, key) is not None


def _sender_sql(timers: list[SenderTimer]) -> Any:
    """SQL pre-filter on the sender; from_address is stored lower-case by the parser."""
    parts = []
    for timer in timers:
        if timer.pattern.startswith("@"):
            domain = timer.pattern[1:].replace("\\", "").replace("%", "").replace("_", "\\_")
            parts += [Mail.from_address.like(f"%@{domain}", escape="\\"),
                      Mail.from_address.like(f"%.{domain}", escape="\\")]
        else:
            parts.append(Mail.from_address == timer.pattern)
    return or_(*parts)


def _sender_plans(session: Session, settings: dict[str, Any], now: datetime, account: Account,
                  account_mode: str, senders: list[SenderTimer],
                  categories: dict[int, Category]) -> list[PlannedDeletion]:
    plans: list[PlannedDeletion] = []
    if not senders:
        return plans
    for key, kind in (("mailbox_delete_days", "mailbox"), ("local_delete_days", "local")):
        relevant = [t for t in senders if getattr(t, key) is not None]
        if not relevant:
            continue
        shortest = min(getattr(t, key) for t in relevant)
        conds = [Mail.account_id == account.id, Mail.category_id.is_not(None),
                 Mail.received_at <= now - timedelta(days=shortest), _sender_sql(relevant)]
        if kind == "mailbox":
            conds += [Mail.folder_role.in_(MANAGED_ROLES), Mail.mailbox_deleted_at.is_(None)]
        for mail in session.scalars(select(Mail).where(*conds)):
            timer = sender_timer_for(senders, mail.from_address)
            if timer is None or getattr(timer, key) is None:
                continue
            days = getattr(timer, key)
            if mail.received_at > now - timedelta(days=days):
                continue
            category = categories.get(mail.category_id)  # type: ignore[arg-type]
            if category is None or protection_reason(mail, category, settings) is not None:
                continue
            plans.append(PlannedDeletion(kind, account, category, days, mail=mail, mode=account_mode,
                                         sender_pattern=timer.pattern))
    return plans


def live_delete_count(plans: list[PlannedDeletion]) -> int:
    """Distinct mails a run would delete for real (mailbox and/or local)."""
    return len({(p.account.id, p.message_id) for p in plans if p.mode == "live"})
