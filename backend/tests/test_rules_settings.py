from datetime import UTC, datetime

import pytest

from mailcat import settings_store
from mailcat.classify.rules import (
    MailFeatures,
    RuleError,
    condition_matches,
    match_rules,
    validate_conditions,
)
from mailcat.worker.scheduler import next_run_at, scheduled_time_for


class R:
    def __init__(self, id, conditions, action="assign", category_id=1, candidates=(), enabled=True):
        self.id, self.name, self.conditions, self.action = id, f"r{id}", conditions, action
        self.category_id, self.candidate_category_ids, self.enabled = category_id, list(candidates), enabled


def feats(**kw):
    base = dict(from_address="ekstre@isbank.com.tr", from_name="Is Bankasi", subject="Kredi Kartı Hesap Özeti",
                to="me@x", body="", headers={"list-unsubscribe": "<u>"}, account="gmail")
    return MailFeatures(**{**base, **kw})


def test_conditions():
    f = feats()
    assert f.from_domain == "isbank.com.tr"
    assert condition_matches(f, {"field": "from_domain", "op": "domain_in", "value": ["isbank.com.tr"]})
    assert condition_matches(feats(from_address="a@mail.isbank.com.tr"),
                             {"field": "from_domain", "op": "domain_in", "value": ["isbank.com.tr"]})
    assert not condition_matches(feats(from_address="a@notisbank.com.tr"),
                                 {"field": "from_domain", "op": "domain_in", "value": ["isbank.com.tr"]})
    assert condition_matches(f, {"field": "subject", "op": "regex", "value": r"hesap [öo]zeti"})
    assert condition_matches(f, {"field": "header:list-unsubscribe", "op": "exists", "value": None})
    assert not condition_matches(f, {"field": "header:list-id", "op": "exists", "value": None})
    assert condition_matches(f, {"field": "account", "op": "equals", "value": "GMAIL"})


def test_first_matching_rule_wins_and_disabled_rules_skip():
    rules = [R(1, [{"field": "subject", "op": "contains", "value": "nope"}]),
             R(2, [{"field": "from_domain", "op": "equals", "value": "isbank.com.tr"}], enabled=False),
             R(3, [{"field": "header:list-unsubscribe", "op": "exists", "value": None}], action="restrict",
               category_id=None, candidates=[4, 5])]
    match = match_rules(feats(), rules)
    assert match.rule_id == 3 and match.action == "restrict" and match.candidate_ids == [4, 5]


def test_validate_conditions():
    clean = validate_conditions([{"field": "from_domain", "op": "domain_in", "value": "A.com, b.com"}])
    assert clean[0]["value"] == ["a.com", "b.com"]
    with pytest.raises(RuleError):
        validate_conditions([])
    with pytest.raises(RuleError):
        validate_conditions([{"field": "subject", "op": "regex", "value": "("}])
    with pytest.raises(RuleError):
        validate_conditions([{"field": "nope", "op": "equals", "value": "x"}])
    with pytest.raises(RuleError):
        validate_conditions([{"field": "header:bad name", "op": "exists"}])


def test_setting_validation():
    assert settings_store.validate("confidence_threshold", 0.7) == 0.7
    assert settings_store.validate("daily_delete_cap", 50.0) == 50
    assert settings_store.validate("protected_senders", [" Boss@X.com ", "@bank.com", ""]) == [
        "@bank.com", "boss@x.com"]
    assert settings_store.validate("blocklist_feeds", {"phishtank": True})["urlhaus"] is True
    for key, bad in [("confidence_threshold", 1.5), ("run_time", "25:00"), ("timezone", "Mars/Base"),
                     ("daily_delete_cap", True), ("protect_flagged", "yes"), ("nope", 1),
                     ("blocklist_feeds", {"evil": True})]:
        with pytest.raises(settings_store.SettingError):
            settings_store.validate(key, bad)


def test_schedule_times():
    settings = {"run_time": "04:00", "timezone": "Europe/Istanbul", "runs_paused": False}
    now = datetime(2026, 10, 3, 0, 30, tzinfo=UTC)  # 03:30 in Istanbul
    slot = scheduled_time_for(now, settings)
    assert slot.hour == 4 and slot.utcoffset().total_seconds() == 3 * 3600
    assert next_run_at(settings, now) == slot
    later = datetime(2026, 10, 3, 2, 0, tzinfo=UTC)  # 05:00 local, past today's slot
    assert next_run_at(settings, later).day == 4
    assert next_run_at({**settings, "runs_paused": True}, now) is None
