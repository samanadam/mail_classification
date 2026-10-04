"""Command line: setup, the worker entry points and maintenance commands. `mailcat --help`."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import select

from mailcat.config import get_config

log = logging.getLogger("mailcat")


def _setup_logging() -> None:
    logging.basicConfig(level=get_config().log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _password(prompt: str, env: str | None) -> str:
    if env:
        value = os.environ.get(env)
        if not value:
            raise SystemExit(f"environment variable {env} is empty")
        return value
    if not sys.stdin.isatty():
        raise SystemExit("no terminal for a password prompt; pass --password-env VAR")
    while True:
        first = getpass.getpass(prompt)
        if len(first) < 10:
            print("too short: use at least 10 characters")
            continue
        if getpass.getpass("again: ") != first:
            print("the two entries differ; try again")
            continue
        return first


def _account(session, name: str):
    from mailcat.models import Account

    account = session.scalar(select(Account).where(Account.name == name))
    if account is None:
        raise SystemExit(f"no account named {name!r}")
    return account


def cmd_migrate(_: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    command.upgrade(cfg, "head")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.seed import seed

    cmd_migrate(args)
    with session_scope() as session:
        seed(session)
    print("database migrated and default categories/rules in place")
    if args.admin:
        return cmd_user_add(argparse.Namespace(username=args.admin, password_env=args.password_env))
    return 0


def cmd_gen_key(_: argparse.Namespace) -> int:
    from mailcat.crypto import generate_key

    print(generate_key())
    return 0


def cmd_user_add(args: argparse.Namespace) -> int:
    from mailcat.api.auth import hash_password
    from mailcat.db import session_scope
    from mailcat.models import User

    password = _password(f"password for {args.username}: ", args.password_env)
    with session_scope() as session:
        user = session.scalar(select(User).where(User.username == args.username))
        if user is None:
            session.add(User(username=args.username, password_hash=hash_password(password)))
            print(f"user {args.username} created")
        else:
            user.password_hash = hash_password(password)
            print(f"password of {args.username} changed")
    return 0


def cmd_account_add(args: argparse.Namespace) -> int:
    from mailcat.crypto import encrypt_credentials
    from mailcat.db import session_scope
    from mailcat.imap.providers import PROVIDERS
    from mailcat.models import Account

    provider = PROVIDERS[args.provider]
    if provider.auth_method == "oauth2":
        if not args.client_id:
            raise SystemExit("Outlook needs --client-id (Azure app registration) and optionally --tenant")
        creds = {"client_id": args.client_id, "tenant": args.tenant or "common"}
    else:
        creds = {"password": _password("app password: ", args.password_env)}
    host = args.host or provider.host
    if not host:
        raise SystemExit("--host is required for a custom account")
    with session_scope() as session:
        session.add(Account(
            name=args.name, provider=args.provider, email=args.email, host=host, port=args.port,
            security=args.security, username=args.username or args.email, auth_method=provider.auth_method,
            credentials_enc=encrypt_credentials(creds), timer_overrides={}, folder_overrides={},
            sync_enabled=True, writeback_enabled=False, writeback_dry_run=True, deletion_enabled=False,
            deletion_dry_run=True, backfill_enabled=False, sorted_parent=args.sorted_parent,
        ))
    print(f"account {args.name} added (read-only until write-back is enabled)")
    if provider.auth_method == "oauth2":
        print(f"next: mailcat account oauth {args.name}")
    return 0


def cmd_account_list(_: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.models import Account

    with session_scope() as session:
        for a in session.scalars(select(Account).order_by(Account.id)):
            print(f"{a.id:3} {a.name:20} {a.provider:8} {a.email:30} sync={a.sync_enabled} "
                  f"writeback={'off' if not a.writeback_enabled else ('dry' if a.writeback_dry_run else 'LIVE')} "
                  f"delete={'off' if not a.deletion_enabled else ('dry' if a.deletion_dry_run else 'LIVE')} "
                  f"last_ok={a.last_sync_ok}")
    return 0


def cmd_account_test(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.worker.accounts import open_session

    with session_scope() as session:
        account = _account(session, args.name)
        with open_session(session, account) as imap:
            print("capabilities:", " ".join(sorted(imap.capabilities)))
            for role in ("inbox", "sent", "spam", "trash", "all"):
                print(f"{role:6} -> {imap.resolve_role(role)}")
            print("delimiter:", imap.delimiter)
    return 0


def cmd_account_oauth(args: argparse.Namespace) -> int:
    from mailcat.crypto import decrypt_credentials, encrypt_credentials
    from mailcat.db import session_scope
    from mailcat.imap.oauth import device_code_login

    with session_scope() as session:
        account = _account(session, args.name)
        creds = device_code_login(decrypt_credentials(account.credentials_enc))
        account.credentials_enc = encrypt_credentials(creds)
    print("refresh token stored")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.models import Run
    from mailcat.worker.run import AlreadyRunning, execute_run
    from mailcat.worker.scheduler import EXIT_BUSY

    try:
        run_id = execute_run(kind=args.kind, triggered_by="cli" if args.kind == "cli" else None,
                             run_id=args.run_id)
    except AlreadyRunning as exc:
        log.warning("%s", exc)
        if args.run_id:
            with session_scope() as session:
                run = session.get(Run, args.run_id)
                if run is not None and run.status == "running":
                    run.status = "aborted"
                    run.errors = [{"step": "run", "message": "another run was in progress"}]
        return EXIT_BUSY
    with session_scope() as session:
        run = session.get(Run, run_id)
        print(json.dumps({"run": run_id, "status": run.status, "counts": run.counts}, default=str))
        return 0 if run.status in ("ok", "partial") else 1


def cmd_scheduler(_: argparse.Namespace) -> int:
    from mailcat.worker.scheduler import loop

    loop()
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("mailcat.api.main:app", host=args.host, port=args.port, proxy_headers=False,
                log_level=get_config().log_level.lower(), access_log=False)
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.worker.undo import undo_run

    with session_scope() as session:
        counts = undo_run(session, args.run_id, include_deletes=not args.moves_only, dry_run=args.dry_run,
                          triggered_by="cli")
    print(json.dumps(counts))
    if not args.dry_run and counts["undone"]:
        print("undone mail is held in place; change its category in the dashboard to release it. "
              "Consider switching the account's write-back to dry-run until the cause is fixed.")
    return 0 if counts["failed"] == 0 else 1


def cmd_reclassify(args: argparse.Namespace) -> int:
    from sqlalchemy import update

    from mailcat.db import session_scope
    from mailcat.models import Category, Mail

    with session_scope() as session:
        conds = [Mail.category_locked.is_(False), Mail.folder_role != "sent"]
        if args.scope == "undecided":
            conds.append(Mail.category_id == select(Category.id).where(
                Category.system_key == "undecided").scalar_subquery())
        elif args.scope == "category":
            cat = session.scalar(select(Category).where(Category.name == args.category))
            if cat is None:
                raise SystemExit(f"no category {args.category!r}")
            conds.append(Mail.category_id == cat.id)
        count = session.execute(update(Mail).where(*conds).values(classified_at=None)).rowcount
    print(f"{count} mails will be re-classified at the next run")
    return 0


def _classifier(name: str | None):
    from mailcat.classify.backends import build_classifier

    return build_classifier(name)


def cmd_eval_export(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.ops.evaluate import export_from_db, export_from_eml

    out = Path(args.out)
    if args.eml_dir:
        n = export_from_eml(Path(args.eml_dir), out)
    else:
        with session_scope() as session:
            account_id = _account(session, args.account).id if args.account else None
            n = export_from_db(session, args.limit, out, account_id)
    print(f"wrote {n} mails to {out}; fill in each \"label\" with a category name")
    return 0


def _print_report(report: dict, path: str | None) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if path:
        Path(path).write_text(text, encoding="utf-8")
    summary = {k: report[k] for k in ("mails", "classifier", "threshold", "coverage", "accuracy_when_decided",
                                      "accuracy_top1_no_threshold", "ms_per_mail", "peak_rss_mb")}
    print(json.dumps(summary, indent=2, default=str))
    for row in report["top_confusions"][:10]:
        print(f"  {row['label']:>18} -> {row['predicted']:<18} x{row['count']}")


def cmd_eval_run(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.ops.evaluate import _load_labeled, evaluate

    rows = _load_labeled(Path(args.file))
    if not rows:
        raise SystemExit("no labeled rows (empty \"label\" fields are skipped)")
    classifier = _classifier(args.backend)
    with session_scope() as session:
        report = evaluate(session, rows, classifier)
    _print_report(report, args.report)
    return 0


def cmd_eval_corrections(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.ops.evaluate import evaluate, rows_from_corrections

    with session_scope() as session:
        rows = rows_from_corrections(session)
        if not rows:
            raise SystemExit("no corrections with content yet")
        report = evaluate(session, rows, _classifier(args.backend))
    _print_report(report, args.report)
    return 0


def cmd_eval_training(args: argparse.Namespace) -> int:
    from mailcat.db import session_scope
    from mailcat.ops.evaluate import export_training_data

    with session_scope() as session:
        n = export_training_data(session, Path(args.out))
    print(f"wrote {n} labeled examples to {args.out}")
    return 0


SAMPLE_MAILS = [
    ("Garanti BBVA <ekstre@garantibbva.com.tr>", "Kredi kartı hesap özetiniz hazır",
     "Değerli müşterimiz, Eylül dönemine ait kredi kartı hesap özetiniz ektedir. Son ödeme tarihi 15 Ekim."),
    ("GitHub <noreply@github.com>", "[GitHub] A new sign-in to your account",
     "We noticed a new sign-in to your account from a new device. If this was you, no action is needed."),
    ("Ayşe <ayse@example.com>", "Cumartesi kahvaltı?", "Selam! Cumartesi sabah kahvaltıya gelir misin? Saat 10 gibi."),
    ("Trendyol <kampanya@trendyol.com>", "Hafta sonuna özel %50 indirim!",
     "Sepette ekstra indirim fırsatlarını kaçırma. Kampanya pazar gecesi sona eriyor."),
]


def cmd_model_download(_: argparse.Namespace) -> int:
    """Fetch the model once (needs network). Daily runs then work with HF_HUB_OFFLINE=1."""
    os.environ["HF_HUB_OFFLINE"] = "0"
    cfg = get_config()
    os.environ["HF_HOME"] = str(cfg.models_path)
    cfg.models_path.mkdir(parents=True, exist_ok=True)
    from mailcat.classify.backends import build_classifier

    classifier = build_classifier()
    print(f"model ready in {cfg.models_path}: {classifier.version}")
    return 0


def cmd_model_check(_: argparse.Namespace) -> int:
    """Load the configured classifier, classify a few samples, report speed and memory."""
    import time

    from mailcat.classify.backends import ModelInput, build_classifier
    from mailcat.classify.engine import CategorySet
    from mailcat.db import session_scope
    from mailcat.ops.evaluate import _peak_rss_mb

    started = time.perf_counter()
    classifier = build_classifier()
    load_s = time.perf_counter() - started
    with session_scope() as session:
        choices = CategorySet.load(session).model_choices()
    inputs = [ModelInput(sender=s, subject=subj, body=b) for s, subj, b in SAMPLE_MAILS]
    started = time.perf_counter()
    outputs = classifier.classify(inputs, choices)
    per_mail = (time.perf_counter() - started) / len(inputs)
    for item, out in zip(inputs, outputs, strict=True):
        name, prob = out.top
        print(f"{item.subject[:45]:45} -> {name} ({prob:.2f}) phishing={out.phishing} action={out.needs_action}")
    print(json.dumps({"classifier": classifier.version, "load_seconds": round(load_s, 1),
                      "ms_per_mail": round(per_mail * 1000), "peak_rss_mb": _peak_rss_mb()}))
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from mailcat import settings_store
    from mailcat.db import session_scope
    from mailcat.ops.backup import create_backup, prune, verify_backup
    from mailcat.worker.alerts import send_alert

    with session_scope() as session:
        keep = int(settings_store.get(session, "backup_keep_days"))
    try:
        path = create_backup()
        result = verify_backup(path)
        removed = prune(keep)
    except Exception as exc:  # noqa: BLE001
        log.exception("backup failed")
        send_alert("Mail categorizer backup failed", [str(exc)[:500]])
        return 1
    print(json.dumps({"backup": str(path), "verify": result, "pruned": [p.name for p in removed]}))
    return 0


def cmd_backup_verify(args: argparse.Namespace) -> int:
    from mailcat.ops.backup import list_backups, verify_backup

    path = Path(args.path) if args.path else (list_backups() or [None])[-1]
    if path is None:
        raise SystemExit("no backups found")
    print(json.dumps(verify_backup(path)))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from mailcat.ops.backup import restore_backup

    if not args.yes:
        raise SystemExit("restore replaces the current database and attachments; add --yes to proceed. "
                         "Stop the api and scheduler containers first.")
    restore_backup(Path(args.path))
    print("restored")
    return 0


def cmd_blocklists(_: argparse.Namespace) -> int:
    from mailcat import settings_store
    from mailcat.db import session_scope
    from mailcat.security import blocklists

    with session_scope() as session:
        settings = settings_store.get_all(session)
    print(json.dumps(blocklists.refresh(settings["blocklist_feeds"], settings.get("phishtank_url") or "")))
    return 0


def cmd_provider_test(args: argparse.Namespace) -> int:
    from mailcat.config import get_config
    from mailcat.db import new_session
    from mailcat.ops.acceptance import TEST_PARENT, run_scenario

    if get_config().read_only:
        raise SystemExit("MAILCAT_READ_ONLY is on. Run this one command with it off:\n"
                         "  docker compose run --rm -e MAILCAT_READ_ONLY=false worker mailcat provider-test ...")
    if not args.yes:
        raise SystemExit(f"This test creates a {TEST_PARENT} folder in the real mailbox, puts test mail in it,\n"
                         "moves and deletes only that test mail, and checks that every other mail is unchanged.\n"
                         "Test mail ends up in Trash. Pause runs in Settings first, then pass --yes.")
    session = new_session()
    try:
        report = run_scenario(session, _account(session, args.name))
    finally:
        session.close()
    for check in report.checks:
        print(f"[{'PASS' if check.ok else 'FAIL'}] {check.name}{'  ' + check.detail if not check.ok else ''}")
    print("ALL PASSED: this provider is safe to enable." if report.ok else "FAILED: keep this account read-only.")
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailcat", description="Local mail categorizer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    p = sub.add_parser("init", help="migrate, seed categories and rules, optionally create the admin user")
    p.add_argument("--admin")
    p.add_argument("--password-env")
    p.set_defaults(func=cmd_init)
    sub.add_parser("gen-key", help="print a new credential encryption key").set_defaults(func=cmd_gen_key)

    user = sub.add_parser("user", help="dashboard users").add_subparsers(dest="user_cmd", required=True)
    p = user.add_parser("add", help="create a user or reset its password")
    p.add_argument("username")
    p.add_argument("--password-env")
    p.set_defaults(func=cmd_user_add)

    acct = sub.add_parser("account", help="mail accounts").add_subparsers(dest="account_cmd", required=True)
    p = acct.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--provider", required=True, choices=["gmail", "icloud", "yandex", "outlook", "custom"])
    p.add_argument("--email", required=True)
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=993)
    p.add_argument("--security", choices=["ssl", "starttls", "plain"], default="ssl")
    p.add_argument("--username")
    p.add_argument("--password-env")
    p.add_argument("--client-id")
    p.add_argument("--tenant")
    p.add_argument("--sorted-parent", default="Sorted")
    p.set_defaults(func=cmd_account_add)
    acct.add_parser("list").set_defaults(func=cmd_account_list)
    p = acct.add_parser("test", help="log in and show detected folders")
    p.add_argument("name")
    p.set_defaults(func=cmd_account_test)
    p = acct.add_parser("oauth", help="Outlook device-code login")
    p.add_argument("name")
    p.set_defaults(func=cmd_account_oauth)

    p = sub.add_parser("run", help="run the daily pipeline once")
    p.add_argument("--kind", default="cli", choices=["cli", "scheduled", "manual"])
    p.add_argument("--run-id", type=int)
    p.set_defaults(func=cmd_run)
    sub.add_parser("scheduler", help="daily schedule and sync-now loop").set_defaults(func=cmd_scheduler)
    p = sub.add_parser("api", help="serve the API and dashboard")
    p.add_argument("--host", default="0.0.0.0")  # noqa: S104 - bound to the tailnet by compose
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("undo", help="reverse the moves and deletes of a run")
    p.add_argument("run_id", type=int)
    p.add_argument("--moves-only", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_undo)
    p = sub.add_parser("reclassify", help="re-classify mail at the next run")
    p.add_argument("--scope", choices=["all", "undecided", "category"], default="undecided")
    p.add_argument("--category")
    p.set_defaults(func=cmd_reclassify)

    ev = sub.add_parser("eval", help="model evaluation (phase 0)").add_subparsers(dest="eval_cmd", required=True)
    p = ev.add_parser("export", help="export mails for hand labelling")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--account")
    p.add_argument("--eml-dir")
    p.set_defaults(func=cmd_eval_export)
    p = ev.add_parser("run", help="evaluate on a labelled JSONL file")
    p.add_argument("file")
    p.add_argument("--backend")
    p.add_argument("--report")
    p.set_defaults(func=cmd_eval_run)
    p = ev.add_parser("corrections", help="evaluate on corrections")
    p.add_argument("--backend")
    p.add_argument("--report")
    p.set_defaults(func=cmd_eval_corrections)
    p = ev.add_parser("training-data", help="export corrections as training data")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_eval_training)

    model = sub.add_parser("model", help="classifier model").add_subparsers(dest="model_cmd", required=True)
    model.add_parser("download", help="download the model once (needs network)").set_defaults(
        func=cmd_model_download)
    model.add_parser("check", help="load the model and classify samples").set_defaults(func=cmd_model_check)

    sub.add_parser("backup", help="encrypted backup, verify, prune").set_defaults(func=cmd_backup)
    p = sub.add_parser("backup-verify", help="check a backup can be decrypted and read")
    p.add_argument("path", nargs="?")
    p.set_defaults(func=cmd_backup_verify)
    p = sub.add_parser("restore", help="restore a backup")
    p.add_argument("path")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)
    sub.add_parser("blocklists", help="refresh blocklist feeds now").set_defaults(func=cmd_blocklists)

    p = sub.add_parser("provider-test", help="run the acceptance checklist in a sandbox folder of a real account")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_provider_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
