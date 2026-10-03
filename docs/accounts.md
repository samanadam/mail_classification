# Adding mail accounts

Every account uses the same IMAP connector. Credentials are encrypted with `MAILCAT_SECRET_KEY` before they are
stored. A new account is **read-only**: sync is on, while write-back and deletion are off.

After adding an account, click **Test connection** on the Accounts page (or run `mailcat account test <name>`).
The test shows the folders it found for Sent, Spam, Trash and (on Gmail) All Mail. If one is wrong or missing,
set a folder override via the API, for example
`PATCH /api/accounts/<id> {"folder_overrides": {"trash": "Deleted Items"}}`.

Before you turn on write-back or deletion for an account, run the acceptance checklist on it. No throwaway
account is needed: the test works inside your real mailbox without touching existing mail.

```bash
# pause runs in Settings first
docker compose run --rm -e MAILCAT_READ_ONLY=false worker mailcat provider-test gmail --yes
```

How it stays away from your mail:

- It runs on a temporary sandbox copy of the account. Your account's settings are never changed.
- The sandbox's IMAP connection can only write inside a new `MailcatTest` folder (and move its own test mail
  to Trash). Any other write is refused by the IMAP client itself, before it reaches the server.
- It appends synthetic test mail to `MailcatTest/Inbox`, runs the real pipeline several times, and checks each
  item of the plan's Testing list against the server.
- Check 15 fingerprints every other folder (UIDs and flags) before and after. If a single existing mail
  moved, vanished or changed read/star state, the test fails.
- Afterwards the test mail is moved to Trash and the `MailcatTest` folders are deleted (only if they hold
  nothing but test mail).

Every line must say PASS, and it ends with `ALL PASSED`. Don't read or move mail in that account while the
test runs (a few minutes): check 15 would report your own changes. Run it once per account.

Provider rules change, so re-check these steps when you set an account up.

## 1. Gmail

1. Turn on 2-Step Verification for the Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. Gmail settings → Forwarding and POP/IMAP → make sure IMAP is enabled. Under *Folder size limits*, keep
   "Do not limit".
4. Add the account with provider **Gmail** and paste the 16-character app password.

Notes:

- Gmail labels act as folders. A move from INBOX to `Sorted/Finance` removes the Inbox label and adds the
  `Sorted/Finance` label. Other labels you set stay.
- Deletion moves the mail to `[Gmail]/Trash`. Gmail would otherwise keep a "deleted" label message in All
  Mail. The worker checks that the mail is gone from All Mail and reports an error if it is not.
- Turkish-language Gmail names folders differently (`[Gmail]/Çöp kutusu`). They are found through
  SPECIAL-USE flags, so no override is needed.

## 2. iCloud

1. Create an app-specific password at <https://account.apple.com> → Sign-In and Security → App-Specific
   Passwords.
2. Provider **iCloud**. The username is your full iCloud address. For some older accounts it is the part
   before `@`; if login fails, try that.
3. Trash is `Deleted Messages`, Spam is `Junk`.

## 3. Yandex

1. Yandex Mail → Settings → Email clients → enable *IMAP*, with "App passwords and OAuth tokens".
2. Create an app password for Mail at <https://id.yandex.com/security/app-passwords>.
3. Provider **Yandex**, host `imap.yandex.com`.

## 4. Custom domain

Use the IMAP host, port and login from your host's documentation. Provider **Custom domain**, security SSL/TLS
(port 993) or STARTTLS (port 143). If the host is not on 993, allow its port in the egress firewall:
`MAILCAT_EXTRA_TCP_PORTS="143" sudo ./deploy/egress-firewall.sh`.

## 5. Outlook / Microsoft 365 (OAuth2)

Microsoft does not allow password logins over IMAP.

1. In the Azure portal, open *App registrations* → *New registration*. Use any name. For supported account
   types, pick personal Microsoft accounts and/or your organization, as fits.
2. Under *Authentication*, enable **Allow public client flows** (needed for device-code login).
3. Under *API permissions*, add *Office 365 Exchange Online* (or *Microsoft Graph*) →
   `IMAP.AccessAsUser.All`, plus `offline_access`.
4. Copy the *Application (client) ID*. Use the tenant `consumers` for an outlook.com/hotmail account, or your
   tenant ID for Microsoft 365.
5. Add the account with provider **Outlook / Microsoft 365**, the client ID and the tenant.
6. Finish the login once from the server:
   ```bash
   docker compose exec worker mailcat account oauth "<account name>"
   ```
   Open the URL it prints on any device and enter the code. The refresh token is stored encrypted. Microsoft
   rotates refresh tokens, and the worker saves the new one after every run.

If the refresh token expires (about 90 days unused, or after a password change), sync fails and you get an
alert. Run the `oauth` command again.
