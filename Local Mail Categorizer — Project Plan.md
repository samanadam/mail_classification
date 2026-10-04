# Local Mail Categorizer — Project Plan

Oct 3, 2026

## Summary

A self-hosted system on the Ubuntu laptop server that reads mail from all accounts, categorizes it with the local Laya model, shows it in a private dashboard, sorts it into folders in the real mailboxes, and deletes old mail on per-category timers. No mail content ever leaves the server.

Principles:

- **Local only.** Classification, storage and link checks all run on the server. Full URLs and mail content are never sent to any outside service.
- **Light on hardware.** One run per day; the model is loaded only during the run. The laptop has 8 GB RAM and hosts other projects.
- **Nothing is hardcoded.** Every timer, threshold and category is a setting editable in the dashboard.
- **Safe by default.** Every action that changes a mailbox starts off, runs in dry-run first, is logged, and is reversible where possible.
- **Read state is yours.** Only you opening a mail marks it read. The system never changes it.

## Architecture

All Python, running as Docker containers beside the existing CasaOS stack, reachable only over Tailscale.

| Component | Technology | Runs | Job |
| --- | --- | --- | --- |
| Worker | Python, `imap-tools`, Laya | Once a day, then exits | Fetch, classify, move, delete, verify |
| API | FastAPI | Always (light) | Serves the dashboard, settings, "sync now" |
| Dashboard | React | Always (static files) | Browse, read, correct, configure |
| Database | PostgreSQL | Always | Mails, categories, settings, logs, full-text search |
| Attachment store | Disk volume, mounted `noexec` | Always | Inert attachment files |
| Antivirus | ClamAV | During the run | Scans new attachments |
| Blocklists | Local copies of phishing/malware feeds | Refreshed daily | Offline link checks |

Flow of one mail:

1. Worker fetches it over IMAP in peek mode (does not mark it read).
2. Body, headers and attachments are stored locally; attachments are scanned.
3. Rules run first; if no rule matches, Laya classifies it and returns a confidence.
4. Links and headers are checked; the `malicious` flag is set if anything fails.
5. The category appears in the dashboard.
6. After the grace period, the mail is moved to its category folder in the mailbox.
7. After the retention period, it is deleted from the mailbox and/or locally, per settings.

## Accounts and authentication

One generic IMAP connector with a config row per account, so a new account is only new credentials. Credentials are stored encrypted.

| Provider | Auth method | Build order | Notes |
| --- | --- | --- | --- |
| Gmail | App password (needs 2-step verification) | 1 | Uses labels; has the delete quirk described under Retention |
| iCloud | App-specific password | 2 | Standard IMAP folders |
| Yandex | App password, IMAP enabled in settings | 3 | Standard IMAP folders |
| Custom domain | Normal IMAP login | 4 | Depends on the host |
| Outlook / Microsoft 365 | OAuth2 via an Azure app registration | 5 | Hardest: needs token refresh handling |

Provider rules change, so each method is verified when that account is set up.

## Categories

Thirteen categories to start. They live in a database table, so adding, renaming or merging is a dashboard edit followed by a re-run.

| Category | What lands there | Main detection |
| --- | --- | --- |
| Work | Mail from the employer and colleagues | Rules (sender domain), then Laya |
| Clients | Freelance clients and project threads | Rules (known senders), then Laya |
| Bank Statements | Monthly account and card summaries only | Rules: bank sender plus subject like "ekstre" or "hesap özeti" |
| Finance | Receipts, invoices, other bank mail, tax, billing | Laya |
| Dev & Infra | GitHub, hosting, domains, server alerts, API providers | Rules (sender), then Laya |
| Security | Login codes, password resets, new-device alerts | Laya |
| Orders & Travel | Shopping, cargo tracking, tickets, bookings | Laya |
| Personal | Real people writing outside work | Laya |
| Newsletters | Subscriptions worth reading | `List-Unsubscribe` header, then Laya |
| Promotions | Marketing and sales mail | `List-Unsubscribe` header, then Laya |
| Social | Notifications from Discord, LinkedIn and similar | Rules (sender) |
| Spam & Malicious | Junk, scams, phishing | Laya plus header and link checks |
| Undecided | Anything below the confidence threshold | Automatic fallback |

Each mail gets exactly one category. Two extra yes/no flags are stored separately: `malicious` (phishing or infected, shown with a red warning) and `needs action`.

## Classification

Laya (`laya-multilingual`, about 421M parameters, Apache 2.0) classifies whatever the rules do not catch. It is a decision model: it returns a category and a probability in one pass instead of generating text, which suits a CPU-only laptop.

- **Model choice:** the multilingual checkpoint, because mail is mixed Turkish and English. Run through ONNX Runtime on CPU if it proves faster.
- **Input:** sender, subject and a trimmed plain-text body. Laya has a short context, so long mails are cut.
- **Rules first:** known senders, bank statement patterns and newsletter headers are decided without the model.
- **Undecided:** if the top confidence is below the threshold (start at 0.6), the mail goes to Undecided instead of getting a weak guess.
- **Threads:** a reply inherits the category of its conversation, so a thread is never split across folders.
- **Corrections:** changing a category in the dashboard, or dragging a mail to another category folder in a mail app, is saved as a correction. Corrections form the test set and later the fine-tuning data.
- **Swappable:** the classifier sits behind one small function. If Laya underperforms, a 3–4B model on Ollama replaces it without touching the rest.
- **Idempotent:** results are keyed by message ID, so everything can be re-classified after a model or category change.

## Daily run and mailbox behaviour

The worker runs once a day (default 04:00, configurable), does everything in one pass, and exits so the model's RAM is freed. A "sync now" button in the dashboard triggers the same run on demand.

Order of one run:

1. Refresh blocklists.
2. Fetch new mail from every enabled account (inbox, sent, and the provider's spam folder).
3. Scan attachments, check links and headers.
4. Classify with rules, then Laya.
5. Move inbox mail older than its grace period into its category folder.
6. Apply retention: delete from mailbox and locally where timers have expired.
7. Verify every move and delete, write logs, send an alert if anything failed.

Mailbox rules:

- **Grace period:** new mail stays in the inbox so it can be seen there. After the grace period (default 1 day, per category) it moves at the next run, read or not. With one run a day, a mail stays in the inbox 24 to 48 hours.
- **Read state is preserved:** read mail stays read and unread mail stays unread after the move, so each folder's unread count shows what is waiting. The worker always fetches in peek mode, and opening a mail in the dashboard does not mark it read in the mailbox.
- **Everything moves:** all categories move, including Spam & Malicious and Undecided, each to its own folder.
- **Folders:** created under one parent, such as `Sorted/Finance`. Gmail uses labels. Existing folders and mail already filed by hand are never touched.
- **Sent mail:** stored locally for thread context and search; never categorized, moved or deleted.
- **Provider spam folder:** ingested read-only, to spot real mail the provider misfiled.
- **Dry-run first:** write-back is off per account until a dry-run period shows the planned moves are right.
- **Undo log:** every move records account, mail, from, to and time, so a bad run can be reversed with one command.

## Retention and deletion

Each category has three independent timers, all editable in the dashboard. Mailbox deletion and local deletion are separate, so a mail can leave Gmail after a month while the local copy stays, or the reverse.

| Category | Move from inbox | Delete from mailbox | Delete local copy |
| --- | --- | --- | --- |
| Finance | 1 day | Never | Never |
| Bank Statements | 1 day | Never | Never |
| Personal | 1 day | 30 days | 30 days |
| Promotions | 1 day | 3 days | 3 days |
| All others | 1 day | Never, until set | Never, until set |

Age is counted from the date the mail was received.

Mailbox deletion is the highest-risk feature, so it has these safeguards:

- **Trash, not permanent delete.** Deleting means moving the mail to the provider's Trash, where it stays recoverable until the provider purges it (typically about 30 days).
- **Gmail quirk.** Over IMAP, deleting from a label folder only removes the label and the mail survives in All Mail. The worker must move it to Gmail's Trash folder explicitly.
- **Verify after every delete.** The worker confirms the mail is gone from its folder and present in Trash; any mismatch is logged as an error and alerted.
- **Preview.** The dashboard lists what will be deleted at the next run, giving a day to rescue anything.
- **Protected mail.** Starred or flagged mail and everything in Undecided is never deleted, whatever the timers say.
- **Daily cap.** If a run wants to delete more than the cap (default 200), it stops and alerts instead.
- **Off by default.** Deletion is enabled per account, only after a dry-run period that logs what it would delete.
- **Deletion log.** Every delete records account, sender, subject, date and the rule that triggered it.

Local deletion:

- **Tombstones.** When a local copy is deleted, its message ID is kept (no content) so the next sync does not download it again.
- **Backups follow retention.** Backups are kept 14 days, so deleted mail does not live on in old backups.

## Security

Full mails and attachments are stored, on the rule that nothing stored is ever executed, rendered unsafely or sent out.

Attachments:

- Stored as inert files under random names on a `noexec` volume; never opened or unpacked by the system.
- Scanned by ClamAV on arrival; hits are flagged and locked.
- Downloaded only by an explicit click, with a warning on risky types (`.exe`, `.js`, `.html`, macro Office files, archives).
- Downloads are blocked for mail flagged malicious.

Mail bodies:

- Plain text by default.
- HTML view is optional, sanitized and sandboxed, with remote images blocked.
- Links are never directly clickable; the real destination is shown first.

Link and sender checks (all local):

- Links are matched against local copies of public phishing and malware blocklists (URLhaus, OpenPhish, PhishTank).
- Heuristics: link text that differs from its destination, lookalike domains, punycode, raw IP addresses, URL shorteners.
- Header checks: SPF/DKIM failures, and display names that do not match the sender's domain.
- Full URLs are never sent to any service, and the system never visits a link.
- Optional later: Google Safe Browsing's hash-prefix API, which does not send the URL itself.

The system:

- Containers run as non-root and are reachable only over Tailscale.
- The dashboard has its own login, in case a device on the tailnet is lost.
- The worker can reach only the mail providers and blocklist feeds; Laya needs no network.
- The data volume is encrypted with LUKS, so a stolen laptop does not expose the mail.
- Mailbox credentials are stored encrypted.

## Dashboard

Version 1 is browse and read only: no sending or replying.

- **Unified inbox** across all accounts, filtered by category, account, read state and flags.
- **Mail view** with the safe rendering described under Security, plus the category, confidence and the reason (rule or model).
- **Review queue** for Undecided and low-confidence mail, with one-click correction.
- **Search** across all accounts, using Postgres full-text search.
- **Deletion preview:** what the next run will delete, with a rescue button.
- **Activity log:** moves, deletes, errors and settings changes.
- **Settings** for everything in the next section.
- **Sync now** button, and the time and result of the last run.

The dashboard shows mail as of the last run.

## Settings

Every value lives in the database, is edited in the dashboard, and takes effect at the next run with no restart.

| Scope | Setting |
| --- | --- |
| Per category | Days before moving out of the inbox |
| Per category | Days before deleting from the mailbox, or Never |
| Per category | Days before deleting the local copy, or Never |
| Per category | Write-back on or off |
| Per account | Sync on or off |
| Per account | Write-back on or off; dry-run on or off |
| Per account | Deletion on or off |
| Per account | Optional overrides of category timers |
| Global | Time of the daily run |
| Global | Confidence threshold for Undecided |
| Global | Daily deletion cap |
| Global | Which mail is protected from deletion |
| Global | Backup lifetime |
| Global | Categories: add, rename, merge, reorder |

Guardrails:

- **Impact shown first.** Shortening a delete timer shows how many mails the change will delete at the next run, and asks for confirmation.
- **Changes are logged.** Each change records what changed, the old and new value, and when.

Open question: whether to add per-sender timers (for example, delete everything from one shop after 3 days) in v1 or later.

## Data model

Main tables in PostgreSQL:

| Table | Holds |
| --- | --- |
| `accounts` | Provider, host, encrypted credentials, per-account switches |
| `categories` | Name, order, the three timers, write-back switch |
| `mails` | Message ID, account, thread ID, sender, subject, received date, body text, current folder, read flag, starred flag, category, confidence, decided-by (rule or model), `malicious`, `needs_action` |
| `attachments` | Mail, original name, stored path, type, size, scan result |
| `links` | Mail, shown text, real destination, check result |
| `rules` | Condition (sender, domain, subject pattern, header) and target category |
| `corrections` | Mail, old category, new category, source (dashboard or mail app), time |
| `actions_log` | Every move and delete: mail, from, to, rule, time, verified or not |
| `tombstones` | Message IDs of locally deleted mail |
| `settings` | Global values |
| `settings_log` | Every settings change, old and new value |
| `runs` | Start, end, counts and errors for each run |

## Reliability

- **Backfill:** the full history is imported once, newest first, throttled at night over several days because providers limit heavy IMAP downloads. It resumes where it stopped. Old mail goes through the same dry-run, preview and cap before anything is moved or deleted.
- **Backups:** an encrypted nightly dump of Postgres and attachments to a second disk, kept 14 days.
- **Alerts:** a Discord webhook message when an account stops syncing, a verify step fails, or the deletion cap is hit.
- **Safe to interrupt:** every step is idempotent, so a power loss mid-run is fixed by the next run.
- **Sync correctness:** mails are tracked by IMAP UID and folder as well as message ID, so mail moved or deleted by hand in a mail app is noticed and handled.

## Build phases

Each phase ends with something usable, and nothing destructive is switched on until the phase that tests it.

| Phase | What gets built | Done when |
| --- | --- | --- |
| 0. Prove Laya | Export about 200 mails, label them by hand, measure accuracy | Accuracy is good enough, or the model is swapped |
| 1. Ingest | Postgres, worker, Gmail read-only fetch in peek mode, attachment store, ClamAV | Gmail mail is stored locally and nothing in the mailbox changed |
| 2. Classify | Rules engine, Laya, confidence, Undecided, thread inheritance | Every stored mail has a category |
| 3. Dashboard | Login, unified inbox, safe mail view, review queue, search, settings | Mail can be browsed and corrected |
| 4. Security checks | Blocklists, link heuristics, header checks, `malicious` flag | Known phishing samples are flagged |
| 5. Write-back | Folder creation, grace-period moves, dry-run, undo log | Moves are correct on a test account, then on Gmail |
| 6. Retention | Timers, Trash-based deletion, verify, preview, cap, tombstones | Deletion tests pass on a test account |
| 7. More accounts | iCloud, Yandex, custom domain, then Outlook (OAuth2) | Each passes the same move and delete tests |
| 8. Backfill and hardening | Full-history import, backups, alerts, LUKS | History is sorted and backups restore cleanly |
| 9. Tune | Fine-tune Laya on corrections if needed | Undecided queue stays small |

## Testing

These checks run against a throwaway account for each provider before that provider's real mailbox is touched.

- [ ] After a full run, every unread mail is still unread.
- [ ] A mail inside its grace period is not moved; one past it is moved to the right folder.
- [ ] A moved mail keeps its read state, star and date.
- [ ] A deleted mail is gone from its folder and present in Trash.
- [ ] On Gmail, a deleted mail is no longer in All Mail.
- [ ] Mail next to a deleted mail is untouched.
- [ ] Running twice moves and deletes nothing extra.
- [ ] Starred, flagged and Undecided mail is never deleted.
- [ ] A run over the deletion cap stops and alerts.
- [ ] The undo command restores a run's moves.
- [ ] A locally deleted mail is not downloaded again.
- [ ] Existing folders and hand-filed mail are untouched.
- [ ] A changed setting takes effect at the next run without a restart.
- [ ] A mail with a blocklisted link or failed SPF/DKIM gets the `malicious` flag.

## Risks and items to verify

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Laya accuracy on Turkish and mixed mail | It is a young model; base accuracy is lower than fine-tuned | Phase 0 test; fine-tune on corrections; swappable classifier |
| Laya project continuity | Small team, new project | Classifier behind one function; Ollama fallback |
| Wrong deletion | The only irreversible action | Trash-only, verify, preview, cap, protected mail, dry-run |
| False positive in Spam & Malicious | A real mail is moved out of the inbox | Moved, never deleted by default; provider spam folder also reviewed |
| Provider auth changes | App passwords and OAuth rules change | Verify at setup; alert when sync stops |
| IMAP throttling during backfill | Providers cap downloads | Slow, resumable backfill |
| Laptop theft or disk failure | It holds all mail | LUKS encryption; encrypted backups |

To confirm at build time: Laya's actual speed and memory on this laptop's CPU, each blocklist feed's terms of use, and each provider's current IMAP and Trash behaviour.
