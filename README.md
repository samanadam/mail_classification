# Local Mail Categorizer

A self-hosted system for the Ubuntu laptop server. It reads mail from all your accounts over IMAP, categorizes
it with the local Laya model, shows it in a private dashboard, files it into folders in the real mailboxes, and
deletes old mail on per-category timers. No mail content ever leaves the server.

The design is in [Local Mail Categorizer — Project Plan.md](Local%20Mail%20Categorizer%20—%20Project%20Plan.md). This
README covers running it.

## Highlights

- **Private by design.** Classification runs on a local model on the server's CPU. No mail content is sent to
  any external service, and the dashboard is reachable only over a Tailscale network.
- **Safe on real mailboxes.** Read state is never changed, nothing is permanently deleted, every move is verified
  by Message-ID, and a hard read-only switch blocks all mailbox writes at the IMAP layer.
- **Provable per provider.** `provider-test` runs the full acceptance checklist inside a sandbox folder of a
  real account and fingerprints every other folder to prove existing mail is untouched.
- **Five providers, one connector.** Gmail, iCloud, Yandex, any IMAP host, and Outlook via OAuth2, with
  SPECIAL-USE folder detection and per-provider fallbacks.
- **Security checks without network calls.** ClamAV scans, offline phishing blocklists, lookalike-domain and
  link-mismatch detection, SPF/DKIM/DMARC parsing, sanitized HTML in a sandboxed iframe.
- **Built for a small server.** One scheduled run per day in a child process, so model memory is returned
  to the OS. It fits next to other projects on an 8 GB laptop.

## Screenshots

All screenshots use invented demo data (`backend/scripts/demo_data.py`); every address is on the reserved
`.example` domain.

![Inbox with categories](docs/screenshots/inbox.jpg)

| | |
| --- | --- |
| ![A phishing mail flagged by the offline checks](docs/screenshots/phishing-detail.jpg) | ![Review queue for low-confidence mail](docs/screenshots/review-queue.jpg) |
| Phishing detection: lookalike domain, SPF failure, link text that hides its destination | Review queue: one click corrects a category and records it for evaluation |
| ![Per-category timers](docs/screenshots/categories-timers.jpg) | ![Accounts with dry-run switches](docs/screenshots/accounts.jpg) |
| Per-category move and delete timers | Per-account write-back and deletion, each with a dry-run mode |

## Stack

| Layer | Stack |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 (full-text search) |
| Mail | imap-tools / imaplib (UID MOVE, UIDPLUS, X-GM-RAW), MSAL for Outlook OAuth2 |
| Classification | Laya multilingual (PyTorch or ONNX on CPU), rules engine, thread inheritance, optional Ollama |
| Frontend | React 18, TypeScript, Vite, TanStack Query |
| Ops | Docker Compose (read-only rootfs, dropped capabilities, internal network), GPG-encrypted backups, Discord alerts |
| Tests | pytest against real PostgreSQL and a GreenMail IMAP server, including the end-to-end acceptance checklist |

## What runs

| Container | Image target | Runs | Job |
| --- | --- | --- | --- |
| `db` | `postgres:16-alpine` | always | mails, categories, settings, logs, full-text search |
| `api` | `api` | always (~80 MB) | dashboard + JSON API, behind Tailscale |
| `worker` | `worker` | always (~60 MB idle) | the scheduler. Each run is a **child process**, so the model, ClamAV and parsing memory go back to the OS when the run exits |

A daily run (default 04:00 Europe/Istanbul, editable in Settings) does, in order:

1. Refreshes the blocklists (URLhaus, OpenPhish, optional PhishTank) and ClamAV signatures.
2. Fetches new mail from every enabled account: INBOX, Sent, and the provider's spam folder. Reads use
   `EXAMINE` and `BODY.PEEK`, so read state is never touched.
3. Stores the body, headers and attachments. Attachments get random names on a `noexec` volume and are
   scanned by ClamAV. Links and headers are checked offline.
4. Classifies with rules, then thread inheritance, then Laya. Below the confidence threshold the mail goes
   to **Undecided**.
5. Moves inbox mail past its grace period into `Sorted/<Category>`. This only happens when write-back is
   on for the account, and the account starts in dry-run.
6. Applies retention: Trash only, verified, with a daily cap, protected mail and a preview. This only happens
   when deletion is on for the account, and the account starts in dry-run.
7. Logs every action and sends a Discord alert when something fails.

"Sync now" in the dashboard queues the same run; it starts within a few seconds.

## First-time setup (on the server)

Requirements: Docker with Compose v2, Tailscale, a LUKS-encrypted data volume, and a second disk for backups.
See [docs/operations.md](docs/operations.md) for setting up LUKS and Tailscale.

```bash
git clone <this repo> /opt/mailcat && cd /opt/mailcat
cp .env.example .env && chmod 600 .env
# fill in POSTGRES_PASSWORD, MAILCAT_BACKUP_PASSPHRASE, MAILCAT_DISCORD_WEBHOOK_URL, paths, BIND_ADDR
docker compose build
docker compose run --rm --no-deps api mailcat gen-key      # paste into MAILCAT_SECRET_KEY in .env
sudo ./deploy/setup-host.sh                                 # data/attachment/backup directories
docker compose run --rm worker mailcat init --admin <name>   # schema, 13 categories, default rules, login
docker compose run --rm worker mailcat model download      # one-time model download; runs stay offline
docker compose run --rm worker mailcat model check         # speed and memory on this CPU
docker compose up -d
sudo ./deploy/egress-firewall.sh                           # optional: only DNS, 443 and 993 outbound
tailscale serve --bg --https=443 http://127.0.0.1:8080      # dashboard at https://<host>.<tailnet>.ts.net
```

Then add accounts in the dashboard under **Accounts**. Instructions for each provider (app passwords, Outlook
OAuth) are in [docs/accounts.md](docs/accounts.md).

## Rolling it out safely (the plan's build phases)

| Step | What to do | Done when |
| --- | --- | --- |
| Start | Deploy with `MAILCAT_READ_ONLY=true` (the default in `.env.example`). No mailbox can be changed, whatever the dashboard says | Dashboard shows the read-only banner |
| 0. Prove Laya | Sync one account read-only, then `mailcat eval export`, label ~200 mails, `mailcat eval run` ([docs/phase0-evaluation.md](docs/phase0-evaluation.md)) | Accuracy is good enough, or set `MAILCAT_CLASSIFIER=ollama` |
| 1–4. Ingest, classify, dashboard, checks | Accounts start read-only. Browse, correct mail in the Review queue, add rules | Every mail has a category |
| 5. Write-back | Run `provider-test` on the account ([docs/accounts.md](docs/accounts.md)); it works in a sandbox folder and proves your existing mail is untouched. Set `MAILCAT_READ_ONLY=false` and restart. Turn on write-back **dry-run**, read the planned moves in Activity, then switch to live | Moves look right |
| 6. Retention | Set timers, turn on deletion **dry-run**, check Activity and the Deletion preview, then switch to live | Deletion log looks right |
| 7. More accounts | Add iCloud, Yandex, custom domain, then Outlook, and run `provider-test` on each | Each passes |
| 8. Backfill | Turn on "Import full history" per account. It is throttled and resumable, newest first | History is sorted |
| 9. Tune | `mailcat eval corrections`, `mailcat eval training-data` | Undecided queue stays small |

The dashboard refuses to switch write-back or deletion to live until the feature has been on in dry-run mode
for at least one finished run.

## Safety guarantees

- **Read-only switch:** with `MAILCAT_READ_ONLY=true` the IMAP client refuses every command that changes a
  mailbox (move, copy, flag, append, expunge, folder create/rename/delete), and runs only log what they would
  do. Dashboard settings cannot override it.
- **Read state is never changed.** Every read uses `EXAMINE` + `BODY.PEEK`. Moves use `UID MOVE`, which keeps
  flags and dates, and every move checks after the fact that `\Seen` and `\Flagged` are unchanged.
- **The tool never deletes mail permanently.** Deleting moves mail to the provider's Trash. Without `MOVE`,
  the fallback runs `UID EXPUNGE` on that one message only, never a plain `EXPUNGE`. On Gmail, each delete is
  also checked against All Mail.
- **Every action is verified by Message-ID** before and after. If a mail is not where the database expects
  it, it is left alone and logged.
- **Your own folders are never touched.** Only INBOX and the tool's own `Sorted/*` folders are managed. Mail
  you file anywhere else becomes "moved by hand" and is not touched again.
- **Protected mail:** starred/flagged mail, Undecided mail, rescued mail and protected senders are never
  deleted.
- **Sender timers** (Categories & timers page): give one sender or domain its own delete timers, for example
  "everything from @shop.com after 3 days". They win over category and account timers, but never over
  protection. Any change that makes more mail deletable shows how many mails are affected and asks you to
  confirm.
- **Daily cap:** a run that wants to delete more than the cap deletes nothing and sends an alert.
- **Undo:** `mailcat undo <run>` moves a run's moves (and Trash deletes) back and holds those mails in place.
- **Safe to interrupt:** every step is idempotent, and a Postgres advisory lock allows one run at a time.

## Everyday commands

All commands run as `docker compose exec worker mailcat <command>`. Use `docker compose run --rm worker …`
when the stack is down.

| Command | Purpose |
| --- | --- |
| `run` | run the pipeline now (same as Sync now) |
| `undo <run_id> [--moves-only] [--dry-run]` | reverse a run's moves and deletes |
| `reclassify --scope all\|undecided\|category --category X` | re-run classification at the next run |
| `account add/list/test/oauth` | manage accounts from the shell |
| `user add <name>` | create a dashboard user or reset a password |
| `eval export/run/corrections/training-data` | model evaluation ([docs/phase0-evaluation.md](docs/phase0-evaluation.md)) |
| `model download/check` | fetch the model once, check speed and memory |
| `provider-test <account> --yes` | the plan's whole testing checklist in a sandbox folder of a real mailbox |
| `backup`, `backup-verify [dir]`, `restore <dir> --yes` | encrypted backups ([docs/operations.md](docs/operations.md)) |
| `blocklists` | refresh the blocklist feeds now |

## Development

```bash
docker compose -f docker-compose.test.yml up -d             # Postgres :55432 and GreenMail IMAP :3143
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # unit, API, and end-to-end IMAP tests (the plan's checklist on GreenMail)
.venv/bin/ruff check mailcat tests
cd ../frontend && npm ci && npm run build                   # npm run dev proxies /api to :8080
```

To try the dashboard without any real mail, load the invented demo data into a separate database (its name
must end in `_demo`) and log in as `demo` / `demo-password-123`:

```bash
docker exec mailcat-testdb psql -U mailcat -d postgres -c "CREATE DATABASE mailcat_demo"
export MAILCAT_DATABASE_URL=postgresql+psycopg://mailcat:mailcat@localhost:55432/mailcat_demo
export MAILCAT_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export MAILCAT_STATIC_DIR=../frontend/dist MAILCAT_READ_ONLY=true
python scripts/demo_data.py && python -m mailcat.cli api --host 127.0.0.1 --port 8099
```

## License

[MIT](LICENSE)

Layout: `backend/mailcat/` holds `imap/` (client, providers, OAuth), `ingest/` (parser, attachments, ClamAV),
`security/` (blocklists, links, headers), `classify/` (rules, engine, model backends), `worker/` (sync,
write-back, retention, run, scheduler, undo), `api/` (FastAPI) and `ops/` (backup, evaluation, acceptance test).
`frontend/` is the React dashboard and `deploy/` holds the host scripts and ClamAV config.
