# Phase 0: proving Laya (and keeping it honest)

The classifier sits behind one interface (`mailcat/classify/backends.py`). This page measures whether Laya is
good enough on your mail. If it is not, switch to another backend:

| `MAILCAT_CLASSIFIER` | What it is |
| --- | --- |
| `laya` | Laya multilingual checkpoint on CPU (default) |
| `laya_onnx` | the same model through ONNX Runtime, using an exported graph at `MAILCAT_LAYA_ONNX_PATH` |
| `ollama` | a 3–4B instruction model on a local Ollama (`MAILCAT_OLLAMA_URL`, `MAILCAT_OLLAMA_MODEL`) |
| `rules_only` | no model: whatever the rules do not catch goes to Undecided |

## 1. Collect and label ~200 mails

Sync an account in read-only mode first (the default for a new account), then:

```bash
docker compose exec worker mailcat eval export --out /data/eval.jsonl --limit 200 --account "Personal Gmail"
docker compose cp worker:/data/eval.jsonl .
```

Each line holds `from`, `subject`, `body` and an empty `"label"`. Fill each label with a category name exactly
as it appears in the dashboard, for example `"Finance"`. Lines with an empty label are skipped. Aim for at
least 10 mails per category you care about, and include hard cases: Turkish and English mixed, bank mail that
is *not* a statement, shop receipts versus promotions.

The file contains mail content, so keep it on the server's encrypted volume.

## 2. Measure

```bash
docker compose cp eval.jsonl worker:/data/eval.jsonl
docker compose exec worker mailcat eval run /data/eval.jsonl --report /data/eval-report.json
```

The report includes:

- `accuracy_when_decided`: how often the category is right when the pipeline did not send the mail to
  Undecided. This is the number that matters for moving mail.
- `coverage`: the share that was not Undecided.
- `accuracy_top1_no_threshold`: accuracy with no Undecided fallback.
- per-category precision and recall, the top confusions, `ms_per_mail` and `peak_rss_mb`.

The real pipeline runs (rules, then the model, then the threshold), so you are measuring what production will
do. Raise *Confidence threshold* in Settings to trade coverage for accuracy.

## 3. Improve before swapping the model

In this order:

1. **Rules** for anything with a stable sender (banks, employer domain, clients). They are free and always
   right.
2. **Category descriptions** (Categories & timers → Edit → *Meaning for the model*). Laya reads them as the
   option definitions, so concrete words help ("Turkcell, Enerjisa, fatura, invoice, receipt").
3. **Threshold:** a higher threshold means fewer wrong moves and more Undecided mail.
4. **Corrections:** every category change in the dashboard, and every drag into another `Sorted/` folder in a
   mail app, is saved. `mailcat eval corrections` re-measures on them.
5. **Calibration or fine-tuning:** `mailcat eval training-data --out /data/train.jsonl` exports corrections as
   labelled data for Laya's calibration (`laya.fit_temperatures`) or fine-tuning.
6. **Swap:** set `MAILCAT_CLASSIFIER=ollama` (or another backend), run `mailcat eval run` again, and
   re-classify with `mailcat reclassify --scope all`.

## Findings from the first smoke test

These came from the multilingual checkpoint on a laptop CPU, in October 2026:

- Load time is ~20 s and classification takes ~0.7–0.8 s per mail.
- Clear-cut mail was right with high confidence: promotions, shipping notices, GitHub security mail.
- Short Turkish personal and work mail had low confidence and went to Undecided. That is the intended
  behaviour, but it means these categories need rules (employer domain) and corrections.
- Laya's generic *phishing* question gave ~1.0 to legitimate marketing and bank mail. The phishing score is
  therefore **advisory by default**: it is stored and shown, but it does not set `malicious`. The `malicious`
  flag comes from the header, link, blocklist and ClamAV checks. Turn on *Let the model's phishing score set
  malicious* only after an evaluation shows it works on your mail.
