from datetime import UTC, datetime

from sqlalchemy import select

from mailcat import settings_store
from mailcat.classify.backends import ModelOutput
from mailcat.classify.engine import CategorySet, classify_mails
from mailcat.models import Rule, Tombstone
from mailcat.worker.policy import live_delete_count, plan_deletions, protection_reason
from tests import factories as f


class FixedClassifier:
    version = "fixed"

    def __init__(self, answers):
        self.answers = answers  # subject -> (category, prob)
        self.seen_choices = []

    def classify(self, inputs, choices):
        self.seen_choices.append(set(choices))
        out = []
        for item in inputs:
            name, prob = self.answers.get(item.subject, (None, 0.0))
            if name is None:
                out.append(ModelOutput({}))
                continue
            rest = (1 - prob) / max(len(choices) - 1, 1)
            out.append(ModelOutput({n: prob if n == name else rest for n in choices}, phishing=0.1,
                                   needs_action=0.8))
        return out

    def close(self):
        pass


def _settings(db):
    return settings_store.get_all(db)


def test_unsure_mail_in_provider_spam_folder_is_spam(db):
    acc = f.account(db)
    junk = f.mail(db, acc, subject="win a prize", folder_role="spam", folder="Junk")
    silent = f.mail(db, acc, subject="no answer", folder_role="spam", folder="Junk")
    real = f.mail(db, acc, subject="Your invoice", folder_role="spam", folder="Junk")
    clf = FixedClassifier({"win a prize": ("Promotions", 0.4), "Your invoice": ("Finance", 0.95)})
    classify_mails(db, [junk, silent, real], clf, _settings(db))
    cats = CategorySet.load(db)
    assert junk.category_id == cats.spam.id and junk.decided_by == "provider" and junk.confidence == 0.4
    assert silent.category_id == cats.spam.id and silent.decided_by == "provider"
    # confidently real mail in the spam folder keeps its category (shown as "provider spam, maybe real")
    assert cats.by_id[real.category_id].name == "Finance"


def test_rules_model_threshold_and_fallback(db):
    acc = f.account(db)
    gh = f.mail(db, acc, from_address="notifications@github.com", subject="PR merged")
    stmt = f.mail(db, acc, from_address="ekstre@isbank.com.tr", subject="Kredi kartı hesap özeti")
    news = f.mail(db, acc, subject="Weekly digest", headers={"list-unsubscribe": "<x>"})
    sure = f.mail(db, acc, subject="Your invoice")
    unsure = f.mail(db, acc, subject="hmm")
    nothing = f.mail(db, acc, subject="no answer")
    sent = f.mail(db, acc, subject="my reply", folder_role="sent", folder="Sent")
    clf = FixedClassifier({"Weekly digest": ("Newsletters", 0.9), "Your invoice": ("Finance", 0.95),
                           "hmm": ("Personal", 0.4), "PR merged": ("Personal", 0.99)})
    counts = classify_mails(db, [gh, stmt, news, sure, unsure, nothing, sent], clf, _settings(db))
    cats = CategorySet.load(db)
    assert cats.by_id[gh.category_id].name == "Dev & Infra" and gh.decided_by == "rule"
    assert cats.by_id[stmt.category_id].name == "Bank Statements"
    assert cats.by_id[news.category_id].name == "Newsletters" and "limited the choices" in news.decision_detail
    assert cats.by_id[sure.category_id].name == "Finance" and sure.confidence == 0.95
    assert unsure.category_id == cats.undecided.id and unsure.decided_by == "threshold"
    assert nothing.category_id == cats.undecided.id and nothing.decided_by == "fallback"
    assert sent.category_id is None and sent.classified_at is None
    assert sure.needs_action is True
    # the restrict rule limited the model's choices for the newsletter
    assert any(c <= {"Newsletters", "Promotions", "Finance", "Orders & Travel", "Security",
                     "Spam & Malicious"} for c in clf.seen_choices)
    # rule-only categories are never offered to the model by default
    assert all("Bank Statements" not in c and "Undecided" not in c for c in clf.seen_choices if len(c) > 6)
    assert counts["rule"] == 2


def test_thread_inheritance_security_override_and_lock(db):
    acc = f.account(db)
    root = f.mail(db, acc, subject="Project kickoff", message_id="<root@x>", thread_id="<root@x>", age_days=2)
    reply = f.mail(db, acc, subject="Re: Project kickoff", thread_id="<root@x>", in_reply_to="<root@x>")
    bad = f.mail(db, acc, subject="Invoice", malicious=True)
    bad.malicious_reasons = ["DKIM fail"]
    locked = f.mail(db, acc, subject="Your invoice", category_locked=True,
                    category_id=f.category(db, "Personal").id)
    clf = FixedClassifier({"Project kickoff": ("Clients", 0.9), "Re: Project kickoff": ("Promotions", 0.99),
                           "Invoice": ("Finance", 0.99), "Your invoice": ("Finance", 0.99)})
    classify_mails(db, [reply, root, bad, locked], clf, _settings(db))
    assert root.category_id == f.category(db, "Clients").id
    assert reply.category_id == root.category_id and reply.decided_by == "thread"
    assert bad.category_id == f.category(db, "Spam & Malicious").id and bad.decided_by == "security"
    assert locked.category_id == f.category(db, "Personal").id


def test_model_phishing_probability_sets_malicious(db):
    acc = f.account(db)
    m = f.mail(db, acc, subject="Verify your account")

    class Phishy(FixedClassifier):
        def classify(self, inputs, choices):
            return [ModelOutput({"Security": 0.9, **{n: 0.01 for n in choices if n != "Security"}},
                                phishing=0.97, needs_action=0.2)]

    classify_mails(db, [m], Phishy({}), _settings(db))
    assert not m.malicious and m.phishing_score == 0.97  # advisory by default
    classify_mails(db, [m], Phishy({}), {**_settings(db), "model_phishing_flags_malicious": True})
    assert m.malicious and "model phishing probability" in m.malicious_reasons[0]
    assert m.category_id == f.category(db, "Spam & Malicious").id


def test_deletion_plan_respects_timers_overrides_and_protections(db):
    acc = f.account(db, deletion_enabled=True, deletion_dry_run=False)
    promo = f.category(db, "Promotions")  # mailbox 3 / local 3 by default
    und = f.category(db, "Undecided")
    old = dict(age_days=10, category_id=promo.id, classified_at=datetime.now(UTC))
    due = f.mail(db, acc, **old)
    flagged = f.mail(db, acc, flagged=True, **old)
    kept = f.mail(db, acc, keep=True, **old)
    fresh = f.mail(db, acc, age_days=1, category_id=promo.id)
    hand_filed = f.mail(db, acc, folder_role="missing", folder=None, uid=None, **old)
    undecided = f.mail(db, acc, age_days=10, category_id=und.id)
    vip = f.mail(db, acc, from_address="boss@corp.com", **old)
    settings = {**_settings(db), "protected_senders": ["@corp.com"]}
    plans = plan_deletions(db, settings, datetime.now(UTC))
    mailbox = {p.mail.id for p in plans if p.kind == "mailbox"}
    local = {p.mail.id for p in plans if p.kind == "local"}
    assert mailbox == {due.id}  # hand-filed mail has no managed location
    assert local == {due.id, hand_filed.id}
    assert protection_reason(flagged, promo, settings) == "starred/flagged"
    assert protection_reason(kept, promo, settings) == "rescued"
    assert protection_reason(undecided, und, settings) == "undecided"
    assert protection_reason(vip, promo, settings) == "protected sender"
    assert fresh.id not in mailbox | local
    assert live_delete_count(plans) == 2

    # per-account override: never delete promotions from this mailbox
    acc.timer_overrides = {str(promo.id): {"mailbox_delete_days": None}}
    db.flush()
    plans = plan_deletions(db, settings, datetime.now(UTC))
    assert not [p for p in plans if p.kind == "mailbox"]

    # a what-if timer for the settings impact check
    plans = plan_deletions(db, settings, datetime.now(UTC),
                           category_overrides={promo.id: {"local_delete_days": None}})
    assert not [p for p in plans if p.kind == "local"]

    # tombstones still get their mailbox timer
    db.add(Tombstone(account_id=acc.id, message_id="<gone@x>", folder_role="sorted", folder="Sorted/Promotions",
                     uid=99, received_at=datetime(2020, 1, 1, tzinfo=UTC), category_id=promo.id))
    acc.timer_overrides = {}
    db.flush()
    plans = plan_deletions(db, settings, datetime.now(UTC))
    assert "<gone@x>" in {p.message_id for p in plans if p.kind == "mailbox"}


def test_disabled_rule_and_dry_run_mode(db):
    acc = f.account(db, deletion_enabled=True, deletion_dry_run=True)
    promo = f.category(db, "Promotions")
    f.mail(db, acc, age_days=10, category_id=promo.id)
    plans = plan_deletions(db, _settings(db), datetime.now(UTC))
    assert plans and all(p.mode == "dry_run" for p in plans) and live_delete_count(plans) == 0
    for rule in db.scalars(select(Rule)):
        rule.enabled = False
    db.flush()
    m = f.mail(db, acc, from_address="x@github.com", subject="s")
    classify_mails(db, [m], FixedClassifier({}), _settings(db))
    assert m.decided_by == "fallback"
