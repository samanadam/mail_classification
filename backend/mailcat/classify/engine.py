"""Decide one category per mail.

Order: correction lock > security override > thread inheritance > rules > model > Undecided.
Results are keyed by message ID, so re-classification after a model or category change is a
matter of clearing `classified_at` and running again.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailcat.classify.backends import Classifier, ModelInput, ModelOutput
from mailcat.classify.rules import MailFeatures, RuleMatch, match_rules
from mailcat.models import Category, Mail, Rule

log = logging.getLogger(__name__)

CLASSIFIABLE_ROLES = ("inbox", "spam", "sorted")
MODEL_BATCH = 32


@dataclass
class CategorySet:
    by_id: dict[int, Category]
    by_name: dict[str, Category]
    undecided: Category
    spam: Category

    @classmethod
    def load(cls, session: Session) -> CategorySet:
        cats = list(session.scalars(select(Category).order_by(Category.position)))
        by_key = {c.system_key: c for c in cats if c.system_key}
        if "undecided" not in by_key or "spam" not in by_key:
            raise RuntimeError("the Undecided and Spam & Malicious categories are missing; run `mailcat init`")
        return cls({c.id: c for c in cats}, {c.name: c for c in cats}, by_key["undecided"], by_key["spam"])

    def model_choices(self, candidate_ids: list[int] | None = None) -> dict[str, str]:
        if candidate_ids:
            cats = [self.by_id[i] for i in candidate_ids if i in self.by_id and i != self.undecided.id]
        else:
            cats = [c for c in self.by_id.values() if c.model_enabled and c.id != self.undecided.id]
        return {c.name: (c.description or c.name) for c in sorted(cats, key=lambda c: c.position)}


def features_of(mail: Mail, account_name: str = "") -> MailFeatures:
    return MailFeatures(
        from_address=mail.from_address or "", from_name=mail.from_name or "", subject=mail.subject or "",
        to=mail.to_addresses or "", body=mail.body_text or "", headers=mail.headers or {},
        account=account_name,
    )


def model_input(mail: Mail, max_chars: int) -> ModelInput:
    sender = f"{mail.from_name} <{mail.from_address}>" if mail.from_name else mail.from_address
    # Bound memory before the model's own cleaner trims quoted text and cuts to max_chars.
    return ModelInput(sender=sender, subject=mail.subject or "", body=(mail.body_text or "")[: max_chars * 4],
                      max_chars=max_chars)


def _thread_parent(session: Session, mail: Mail) -> Mail | None:
    if mail.thread_id == mail.message_id and not mail.in_reply_to:
        return None
    return session.scalar(
        select(Mail)
        .where(
            Mail.account_id == mail.account_id,
            Mail.thread_id == mail.thread_id,
            Mail.id != mail.id,
            Mail.folder_role.in_(CLASSIFIABLE_ROLES),
            Mail.category_id.is_not(None),
            Mail.decided_by.in_(("rule", "model", "correction")),
            Mail.malicious.is_(False),
        )
        .order_by(Mail.received_at)
        .limit(1)
    )


def _apply(mail: Mail, category: Category, confidence: float | None, decided_by: str, detail: str) -> None:
    mail.category_id = category.id
    mail.confidence = None if confidence is None else round(float(confidence), 4)
    mail.decided_by = decided_by
    mail.decision_detail = detail[:1000]


def classify_mails(
    session: Session,
    mails: list[Mail],
    classifier: Classifier,
    settings: dict[str, Any],
    categories: CategorySet | None = None,
    account_names: dict[int, str] | None = None,
) -> dict[str, int]:
    """Classify the given mails in place. Returns counts by decision source."""
    categories = categories or CategorySet.load(session)
    rules = list(session.scalars(select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.position, Rule.id)))
    threshold = float(settings["confidence_threshold"])
    max_chars = int(settings["body_max_chars"])
    account_names = account_names or {}
    counts: dict[str, int] = defaultdict(int)

    # Oldest first, so a thread's first mail is decided before its replies look it up.
    todo = sorted((m for m in mails if m.folder_role in CLASSIFIABLE_ROLES), key=lambda m: m.received_at)
    matches: dict[int, RuleMatch | None] = {
        m.id: match_rules(features_of(m, account_names.get(m.account_id, "")), rules) for m in todo
    }

    # Group by the candidate set so each model call shares one question set.
    groups: dict[tuple[int, ...], list[Mail]] = defaultdict(list)
    for mail in todo:
        match = matches[mail.id]
        key = tuple(sorted(match.candidate_ids)) if match and match.action == "restrict" else ()
        groups[key].append(mail)

    outputs: dict[int, ModelOutput] = {}
    for key, group in groups.items():
        choices = categories.model_choices(list(key) or None)
        if not choices:
            continue
        for start in range(0, len(group), MODEL_BATCH):
            batch = group[start:start + MODEL_BATCH]
            try:
                results = classifier.classify([model_input(m, max_chars) for m in batch], choices)
            except Exception:  # noqa: BLE001 - a model failure must not stop the run
                log.exception("model failed on a batch of %d mails", len(batch))
                results = [ModelOutput(probabilities={}) for _ in batch]
            for mail, out in zip(batch, results, strict=True):
                outputs[mail.id] = out

    now = datetime.now(UTC)
    for mail in todo:
        out = outputs.get(mail.id)
        match = matches[mail.id]
        if out is not None:
            mail.probabilities = {k: round(v, 4) for k, v in out.probabilities.items()} or None
            mail.phishing_score = None if out.phishing is None else round(out.phishing, 4)
            if out.needs_action is not None:
                mail.needs_action = out.needs_action >= float(settings["needs_action_threshold"])
            model_flags = bool(settings.get("model_phishing_flags_malicious"))
            if model_flags and out.phishing is not None and out.phishing >= float(settings["phishing_threshold"]):
                if not mail.malicious:
                    mail.malicious = True
                    mail.malicious_reasons = [*(mail.malicious_reasons or []),
                                              f"model phishing probability {out.phishing:.2f}"]
        mail.classified_at = now
        mail.classifier_version = classifier.version

        if mail.category_locked:
            counts["locked"] += 1
            continue
        if mail.malicious:
            reasons = "; ".join(mail.malicious_reasons or []) or "security checks failed"
            _apply(mail, categories.spam, 1.0, "security", reasons)
            counts["security"] += 1
            continue
        if settings.get("thread_inheritance", True):
            parent = _thread_parent(session, mail)
            if parent is not None and parent.category_id in categories.by_id:
                _apply(mail, categories.by_id[parent.category_id], parent.confidence, "thread",
                       f"reply in a thread filed as {categories.by_id[parent.category_id].name}")
                counts["thread"] += 1
                continue
        if match and match.action == "assign" and match.category_id in categories.by_id:
            _apply(mail, categories.by_id[match.category_id], 1.0, "rule", f"rule: {match.rule_name}")
            counts["rule"] += 1
            continue
        name, prob = out.top if out else (None, 0.0)
        via = f" (rule {match.rule_name} limited the choices)" if match and match.action == "restrict" else ""
        if name is None or name not in categories.by_name:
            _apply(mail, categories.undecided, None, "fallback", "no model answer" + via)
            counts["fallback"] += 1
        elif prob < threshold:
            _apply(mail, categories.undecided, prob, "threshold",
                   f"best guess {name} at {prob:.2f} is below {threshold:.2f}{via}")
            counts["undecided"] += 1
        else:
            _apply(mail, categories.by_name[name], prob, "model", f"model: {name} {prob:.2f}{via}")
            counts["model"] += 1
    return dict(counts)
