"""Deterministic rules evaluated before the model."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

FIELDS = ("from_address", "from_domain", "from_name", "subject", "to", "body", "account")
OPS = ("equals", "contains", "starts_with", "ends_with", "regex", "domain_in", "in", "exists",
       "not_exists")
LIST_OPS = ("domain_in", "in")
VALUELESS_OPS = ("exists", "not_exists")


class RuleError(ValueError):
    pass


@dataclass
class MailFeatures:
    from_address: str
    from_name: str
    subject: str
    to: str
    body: str
    headers: dict[str, Any] = field(default_factory=dict)
    account: str = ""

    @property
    def from_domain(self) -> str:
        return self.from_address.rsplit("@", 1)[-1].lower() if "@" in self.from_address else ""


@dataclass
class RuleMatch:
    rule_id: int
    rule_name: str
    action: str  # assign | restrict
    category_id: int | None
    candidate_ids: list[int]


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def validate_conditions(conditions: Any) -> list[dict[str, Any]]:
    if not isinstance(conditions, list) or not conditions:
        raise RuleError("a rule needs at least one condition")
    clean: list[dict[str, Any]] = []
    for cond in conditions:
        if not isinstance(cond, dict):
            raise RuleError("each condition must be an object")
        fld, op, value = cond.get("field"), cond.get("op"), cond.get("value")
        if not isinstance(fld, str) or not (fld in FIELDS or fld.startswith("header:")):
            raise RuleError(f"unknown field {fld!r}")
        if fld.startswith("header:") and not re.fullmatch(r"header:[A-Za-z0-9\-]+", fld):
            raise RuleError(f"invalid header field {fld!r}")
        if op not in OPS:
            raise RuleError(f"unknown operator {op!r}")
        if op in VALUELESS_OPS:
            value = None
        elif op in LIST_OPS:
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",")]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise RuleError(f"{op} needs a list of values")
            value = [v.strip().lower() for v in value if v.strip()]
            if not value:
                raise RuleError(f"{op} needs at least one value")
        else:
            if not isinstance(value, str) or not value:
                raise RuleError(f"{op} needs a value")
            if op == "regex":
                try:
                    _compiled(value)
                except re.error as exc:
                    raise RuleError(f"invalid regular expression: {exc}") from exc
        clean.append({"field": fld.lower() if fld.startswith("header:") else fld, "op": op, "value": value})
    return clean


def _field_value(features: MailFeatures, fld: str) -> str | None:
    if fld.startswith("header:"):
        raw = features.headers.get(fld[7:].lower())
        if raw is None:
            return None
        return " ".join(raw) if isinstance(raw, list) else str(raw)
    return {
        "from_address": features.from_address,
        "from_domain": features.from_domain,
        "from_name": features.from_name,
        "subject": features.subject,
        "to": features.to,
        "body": features.body,
        "account": features.account,
    }.get(fld)


def condition_matches(features: MailFeatures, cond: dict[str, Any]) -> bool:
    value = _field_value(features, cond["field"])
    op = cond["op"]
    if op == "exists":
        return bool(value)
    if op == "not_exists":
        return not value
    if value is None:
        return False
    text = value.lower()
    target = cond.get("value")
    if op == "equals":
        return text == str(target).lower()
    if op == "contains":
        return str(target).lower() in text
    if op == "starts_with":
        return text.startswith(str(target).lower())
    if op == "ends_with":
        return text.endswith(str(target).lower())
    if op == "regex":
        return bool(_compiled(str(target)).search(value))
    if op == "in":
        return text in (target or [])
    if op == "domain_in":
        return any(text == d or text.endswith("." + d) for d in (target or []))
    return False


def match_rules(features: MailFeatures, rules: list[Any]) -> RuleMatch | None:
    """`rules` are ORM Rule objects (or anything with the same attributes), in priority order."""
    for rule in rules:
        if not rule.enabled or not rule.conditions:
            continue
        if all(condition_matches(features, c) for c in rule.conditions):
            return RuleMatch(
                rule_id=rule.id, rule_name=rule.name, action=rule.action,
                category_id=rule.category_id,
                candidate_ids=[int(i) for i in (rule.candidate_category_ids or [])],
            )
    return None
