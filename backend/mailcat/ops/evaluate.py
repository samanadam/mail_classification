"""Phase 0 and ongoing model evaluation.

1. `mailcat eval export` writes ~200 mails to JSONL with an empty "label" field.
2. Label them by hand (category names exactly as in the dashboard).
3. `mailcat eval run labeled.jsonl` classifies them with the real pipeline (rules, model,
   threshold) and reports accuracy, coverage, per-category scores, speed and memory.

`mailcat eval corrections` does the same with dashboard/mail-app corrections as the test set.
"""
from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.classify.backends import Classifier
from mailcat.classify.engine import CategorySet, classify_mails
from mailcat.ingest.parser import parse_message
from mailcat.models import Category, Correction, Mail


def _peak_rss_mb() -> float | None:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / 1024 if sys.platform != "darwin" else peak / 1024 / 1024, 1)
    except ImportError:
        return None


def export_from_db(session: Session, limit: int, out: Path, account_id: int | None = None,
                   seed: int = 7) -> int:
    query = select(Mail).where(Mail.folder_role.in_(("inbox", "sorted", "spam")))
    if account_id:
        query = query.where(Mail.account_id == account_id)
    mails = list(session.scalars(query.order_by(Mail.received_at.desc()).limit(limit * 5)))
    random.Random(seed).shuffle(mails)
    cats = {c.id: c.name for c in session.scalars(select(Category))}
    with out.open("w", encoding="utf-8") as handle:
        for mail in mails[:limit]:
            handle.write(json.dumps({
                "id": mail.message_id, "from": f"{mail.from_name} <{mail.from_address}>",
                "subject": mail.subject, "body": (mail.body_text or "")[:6000],
                "headers": {k: v for k, v in (mail.headers or {}).items()
                            if k in ("list-unsubscribe", "list-id", "authentication-results")},
                "current": cats.get(mail.category_id) if mail.category_id else None,
                "label": "",
            }, ensure_ascii=False) + "\n")
    return min(limit, len(mails))


def export_from_eml(directory: Path, out: Path) -> int:
    count = 0
    with out.open("w", encoding="utf-8") as handle:
        for path in sorted(directory.glob("*.eml")):
            parsed = parse_message(path.read_bytes())
            handle.write(json.dumps({
                "id": parsed.message_id, "from": f"{parsed.from_name} <{parsed.from_address}>",
                "subject": parsed.subject, "body": parsed.body_text[:6000],
                "headers": {k: v for k, v in parsed.headers.items()
                            if k in ("list-unsubscribe", "list-id", "authentication-results")},
                "current": None, "label": "",
            }, ensure_ascii=False) + "\n")
            count += 1
    return count


def _load_labeled(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not row.get("label"):
            continue
        row["_line"] = line_no
        rows.append(row)
    return rows


def _transient_mail(row: dict[str, Any], fake_id: int) -> Mail:
    name, address = parseaddr(row.get("from", ""))
    mail = Mail(account_id=0, message_id=row.get("id") or f"<eval-{fake_id}>", thread_id=f"<eval-{fake_id}>",
                folder_role="inbox", from_name=name, from_address=address.lower(),
                subject=row.get("subject", ""), body_text=row.get("body", ""), headers=row.get("headers") or {},
                to_addresses="", received_at=datetime.now(UTC), malicious=False, malicious_reasons=[],
                needs_action=False, category_locked=False)
    mail.id = -fake_id
    return mail


def evaluate(session: Session, rows: list[dict[str, Any]], classifier: Classifier) -> dict[str, Any]:
    categories = CategorySet.load(session)
    unknown = sorted({r["label"] for r in rows if r["label"] not in categories.by_name})
    if unknown:
        raise ValueError(f"labels that are not category names: {', '.join(unknown)}")
    settings = {**settings_store.get_all(session), "thread_inheritance": False}
    mails = [_transient_mail(r, i + 1) for i, r in enumerate(rows)]
    started = time.perf_counter()
    with session.no_autoflush:
        classify_mails(session, mails, classifier, settings, categories)
    elapsed = time.perf_counter() - started

    threshold = float(settings["confidence_threshold"])
    confusion: Counter[tuple[str, str]] = Counter()
    per_cat: dict[str, Counter] = defaultdict(Counter)
    decided = correct = top1_correct = 0
    by_source: Counter[str] = Counter()
    errors = []
    for row, mail in zip(rows, mails, strict=True):
        truth = row["label"]
        predicted = categories.by_id[mail.category_id].name if mail.category_id else "Undecided"
        by_source[mail.decided_by or "none"] += 1
        # What the pipeline would say with no Undecided fallback.
        if mail.decided_by == "threshold" and mail.probabilities:
            forced = max(mail.probabilities, key=lambda k: mail.probabilities[k])
        else:
            forced = predicted
        top1_correct += forced == truth
        per_cat[truth]["support"] += 1
        if predicted == "Undecided" and truth != "Undecided":
            per_cat[truth]["undecided"] += 1
            continue
        decided += 1
        if predicted == truth:
            correct += 1
            per_cat[truth]["tp"] += 1
        else:
            per_cat[truth]["fn"] += 1
            per_cat[predicted]["fp"] += 1
            confusion[(truth, predicted)] += 1
            errors.append({"line": row["_line"], "subject": row.get("subject", "")[:120], "label": truth,
                           "predicted": predicted, "confidence": mail.confidence, "by": mail.decided_by})
    report_cats = {}
    for name, c in sorted(per_cat.items()):
        precision = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None
        recall = c["tp"] / c["support"] if c["support"] else None
        report_cats[name] = {"support": c["support"], "precision": precision, "recall": recall,
                             "undecided": c["undecided"]}
    n = len(rows)
    return {
        "mails": n,
        "classifier": classifier.version,
        "threshold": threshold,
        "coverage": decided / n if n else 0,
        "accuracy_when_decided": correct / decided if decided else None,
        "accuracy_top1_no_threshold": top1_correct / n if n else None,
        "decided_by": dict(by_source),
        "seconds_total": round(elapsed, 2),
        "ms_per_mail": round(1000 * elapsed / n, 1) if n else None,
        "peak_rss_mb": _peak_rss_mb(),
        "per_category": report_cats,
        "top_confusions": [{"label": a, "predicted": b, "count": c} for (a, b), c in confusion.most_common(15)],
        "errors": errors[:100],
    }


def rows_from_corrections(session: Session) -> list[dict[str, Any]]:
    cats = {c.id: c.name for c in session.scalars(select(Category))}
    rows = []
    for i, corr in enumerate(session.scalars(select(Correction).where(Correction.features.is_not(None))
                                             .order_by(Correction.id))):
        feats = corr.features or {}
        label = cats.get(corr.new_category_id)
        if not label:
            continue
        rows.append({"id": corr.message_id, "from": feats.get("sender", ""), "subject": feats.get("subject", ""),
                     "body": feats.get("body", ""), "headers": feats.get("headers", {}), "label": label,
                     "_line": i + 1})
    # The latest correction of a mail wins.
    latest = {r["id"]: r for r in rows}
    return list(latest.values())


def export_training_data(session: Session, out: Path) -> int:
    """Corrections as JSONL (state + label), the input for calibration or fine-tuning."""
    rows = rows_from_corrections(session)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({"from": row["from"], "subject": row["subject"], "body": row["body"],
                                     "label": row["label"]}, ensure_ascii=False) + "\n")
    return len(rows)
