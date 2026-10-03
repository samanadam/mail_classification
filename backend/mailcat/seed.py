"""Initial categories and rules from the project plan. Idempotent: only inserts what is missing,
so user edits are never overwritten."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mailcat.models import Category, Rule

# name, description for the model, system_key, model_enabled, move, mailbox_delete, local_delete, color
DEFAULT_CATEGORIES: list[tuple[str, str, str | None, bool, int, int | None, int | None, str]] = [
    ("Work", "mail from my employer and colleagues about my job", None, True, 1, None, None, "#2563eb"),
    ("Clients", "freelance clients, project threads, quotes and deliverables", None, True, 1, None, None,
     "#7c3aed"),
    ("Bank Statements", "monthly bank account or credit card statements (ekstre, hesap özeti)", None,
     False, 1, None, None, "#0f766e"),
    ("Finance", "receipts, invoices, payments, bank notices, taxes, bills and subscriptions billing",
     None, True, 1, None, None, "#059669"),
    ("Dev & Infra", "GitHub, hosting, domains, servers, monitoring alerts, API and cloud providers",
     None, True, 1, None, None, "#475569"),
    ("Security", "login codes, verification codes, password resets, new device or sign-in alerts",
     None, True, 1, None, None, "#b45309"),
    ("Orders & Travel", "online shopping orders, shipping and cargo tracking, tickets, hotel or flight "
     "bookings", None, True, 1, None, None, "#c2410c"),
    ("Personal", "a real person writing to me personally, friends and family, outside work", None, True,
     1, 30, 30, "#db2777"),
    ("Newsletters", "newsletters and content subscriptions worth reading", None, True, 1, None, None,
     "#0891b2"),
    ("Promotions", "marketing, advertising, discounts, sales campaigns", None, True, 1, 3, 3, "#ca8a04"),
    ("Social", "notifications from social networks and chat apps such as LinkedIn or Discord", None,
     True, 1, None, None, "#4f46e5"),
    ("Spam & Malicious", "junk, scams, phishing, fraud, unsolicited bulk mail", "spam", True, 1, None,
     None, "#dc2626"),
    ("Undecided", "below the confidence threshold", "undecided", False, 1, None, None, "#6b7280"),
]

DEV_DOMAINS = [
    "github.com", "gitlab.com", "bitbucket.org", "vercel.com", "netlify.com", "cloudflare.com",
    "digitalocean.com", "hetzner.com", "hetzner.de", "namecheap.com", "godaddy.com", "porkbun.com",
    "supabase.io", "supabase.com", "sentry.io", "uptimerobot.com", "amazonaws.com", "tailscale.com",
    "docker.com", "npmjs.com", "pypi.org", "heroku.com", "render.com", "fly.io", "railway.app",
]
SOCIAL_DOMAINS = [
    "linkedin.com", "discord.com", "discordapp.com", "facebookmail.com", "x.com", "twitter.com",
    "instagram.com", "redditmail.com", "pinterest.com", "tiktok.com", "quora.com",
]
BANK_DOMAINS = [
    "garantibbva.com.tr", "garanti.com.tr", "isbank.com.tr", "akbank.com", "yapikredi.com.tr",
    "ziraatbank.com.tr", "qnb.com.tr", "enpara.com", "denizbank.com", "halkbank.com.tr",
    "vakifbank.com.tr", "ing.com.tr", "teb.com.tr", "kuveytturk.com.tr", "albaraka.com.tr",
]
STATEMENT_SUBJECT = r"(ekstre|hesap [öo]zeti|kart [öo]zeti|statement)"


def seed(session: Session) -> None:
    existing = {c.name for c in session.scalars(select(Category))}
    for pos, (name, desc, key, model_on, move, mbox, local, color) in enumerate(DEFAULT_CATEGORIES):
        if name in existing:
            continue
        if key and session.scalar(select(Category).where(Category.system_key == key)):
            continue
        session.add(Category(
            name=name, description=desc, position=pos, system_key=key, model_enabled=model_on,
            move_days=move, mailbox_delete_days=mbox, local_delete_days=local, color=color,
        ))
    session.flush()

    if session.scalar(select(func.count()).select_from(Rule)):
        return
    by_name = {c.name: c.id for c in session.scalars(select(Category))}

    def add(name: str, pos: int, conditions: list[dict], category: str | None = None,
            candidates: list[str] | None = None) -> None:
        if candidates:
            ids = [by_name[c] for c in candidates if c in by_name]
            session.add(Rule(name=name, position=pos, conditions=conditions, action="restrict",
                             candidate_category_ids=ids))
        elif category in by_name:
            session.add(Rule(name=name, position=pos, conditions=conditions, action="assign",
                             category_id=by_name[category]))

    add("Bank statements", 10, [
        {"field": "from_domain", "op": "domain_in", "value": BANK_DOMAINS},
        {"field": "subject", "op": "regex", "value": STATEMENT_SUBJECT},
    ], category="Bank Statements")
    add("Dev & infra senders", 20, [{"field": "from_domain", "op": "domain_in", "value": DEV_DOMAINS}],
        category="Dev & Infra")
    add("Social networks", 30, [{"field": "from_domain", "op": "domain_in", "value": SOCIAL_DOMAINS}],
        category="Social")
    add("Bulk mail (List-Unsubscribe)", 90, [{"field": "header:list-unsubscribe", "op": "exists"}],
        candidates=["Newsletters", "Promotions", "Finance", "Orders & Travel", "Security",
                    "Spam & Malicious"])
