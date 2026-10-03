# Operations

## Disk encryption (LUKS)

All mail lives under `MAILCAT_ROOT`: the Postgres files, attachments, the model cache and the blocklists.
`MAILCAT_ROOT` must sit on a LUKS volume, so a stolen laptop does not expose any mail. `setup-host.sh` warns you
if it is not.

Example with a dedicated partition `/dev/sdX3` (**this erases the partition**):

```bash
sudo cryptsetup luksFormat --type luks2 /dev/sdX3
sudo cryptsetup open /dev/sdX3 mailcat_crypt
sudo mkfs.ext4 -L mailcat /dev/mapper/mailcat_crypt
sudo mkdir -p /srv/mailcat && sudo mount /dev/mapper/mailcat_crypt /srv/mailcat
```

To unlock it at boot, add a line to `/etc/crypttab` (`mailcat_crypt UUID=<luks uuid> none luks`) and a line to
`/etc/fstab` (`/dev/mapper/mailcat_crypt /srv/mailcat ext4 defaults,nofail 0 2`). On a headless server, enter
the passphrase over SSH with `dropbear-initramfs`. A key file on the root disk works only if the root disk is
itself encrypted. Without it, a key file defeats the point.

Start the stack only after the volume is mounted. Otherwise Docker would create empty directories on the bare
disk and Postgres would start a fresh database there. Disable Docker's own restart of these containers at boot
(`docker compose down` before reboots, or use the unit below), and start the stack from a systemd unit with
`RequiresMountsFor=/srv/mailcat` that runs `docker compose up -d` in `/opt/mailcat`. `deploy/mailcat.service` is
that unit; set `restart: "no"` in a `docker-compose.override.yml` so only the unit starts the stack:

```bash
sudo cp deploy/mailcat.service /etc/systemd/system/ && sudo systemctl enable mailcat
```

Put the backup disk (`MAILCAT_BACKUP_ROOT`) on LUKS too. Backups are additionally encrypted with GnuPG.

## Network access (Tailscale)

The dashboard binds to `BIND_ADDR`, which must never be `0.0.0.0`.

- **Recommended:** set `BIND_ADDR=127.0.0.1` and `MAILCAT_COOKIE_SECURE=true`, then run
  `tailscale serve --bg --https=443 http://127.0.0.1:8080`. The dashboard is then available only over HTTPS on
  your tailnet.
- **Alternative:** set `BIND_ADDR=<this machine's 100.x Tailscale IP>` and `MAILCAT_COOKIE_SECURE=false`.
  WireGuard encrypts the traffic, but the browser sees plain HTTP.

The dashboard has its own login, in case a device on your tailnet is lost. Failed logins are throttled (5 per
15 minutes per user and per IP). Sessions last 7 days (`MAILCAT_SESSION_HOURS`). Use Tailscale ACLs to restrict
which of your devices can reach the server.

## Outbound traffic

`deploy/egress-firewall.sh` lets the containers reach only DNS, HTTPS (443) and IMAPS (993):

```bash
sudo cp -r . /opt/mailcat   # if not already there
sudo cp deploy/mailcat-egress.service /etc/systemd/system/
sudo systemctl enable --now mailcat-egress
```

The model never needs the network after `mailcat model download`; the worker image sets `HF_HUB_OFFLINE=1`.
URLs from mail are never fetched or sent anywhere. The blocklists are downloaded whole and matched locally.

## Backups

The scheduler runs `mailcat backup` after each daily run (unless *Settings → Backups* is off). A backup is:

- `pg_dump --format=custom` piped into `gpg --symmetric --cipher-algo AES256` (passphrase
  `MAILCAT_BACKUP_PASSPHRASE`),
- a tar of the attachment store, piped into gpg the same way,
- a `manifest.json` with SHA-256 checksums.

Each backup is verified straight after it is written: the checksums are checked, the database dump is
decrypted into `pg_restore --list`, and the attachment archive is listed. Backups older than *Backup lifetime*
(default 14 days) are pruned, so deleted mail does not live on in old backups. The newest backup is never
pruned. A failed backup sends an alert.

**Restore drill** (do it once after setup, then every few months):

```bash
docker compose exec worker mailcat backup-verify                 # newest backup
docker compose stop api worker
docker compose run --rm worker mailcat restore /backup/<timestamp> --yes
docker compose up -d
```

Keep the passphrase and `MAILCAT_SECRET_KEY` in your password manager. Without the passphrase the backups are
useless. Without the secret key, every account password must be entered again.

## Alerts

Set `MAILCAT_DISCORD_WEBHOOK_URL`. You get an alert when:

- an account fails to sync (after *Alert after N failed syncs*, default 1),
- a move or delete fails its verification,
- a run wants to delete more than the daily cap,
- a run crashes or the model cannot be loaded,
- a backup fails.

Alerts contain counts and error text, never mail content. Turn on *Send an alert after every run* to get a
daily heartbeat.

## Undoing a bad run

```bash
docker compose exec worker mailcat undo <run id> --dry-run     # what would be reversed
docker compose exec worker mailcat undo <run id>
```

Undo moves mail back to the folder it came from and takes Trash deletes back out of Trash. Undone mail is held
in place: write-back leaves it alone until you change its category or click *Release* in the mail view. Switch
the account's write-back to dry-run until you have found the cause.

## Upgrades

```bash
cd /opt/mailcat && git pull
docker compose build
docker compose run --rm worker mailcat migrate
docker compose up -d
```

Runs that were interrupted (a power loss, a container restart) are marked *aborted* when the scheduler starts,
and the next run picks up the work.

## Resource use on the 8 GB laptop

- Idle: Postgres ~60 MB, the API ~80 MB, the scheduler ~60 MB.
- During a run: expect roughly 1–2 GB for the Laya multilingual checkpoint on CPU, and about 1 GB for clamd
  with signatures loaded. Both exist only inside the run's child process, and `WORKER_MEM_LIMIT` (default 3g)
  caps the total. `mailcat model check` prints the measured peak memory on Linux.
- Speed: measured at ~0.7–0.8 s per mail on a laptop CPU, for three questions per mail. Check yours with
  `mailcat model check`. If it is too slow, try `MAILCAT_CLASSIFIER=laya_onnx` with an exported ONNX graph at
  `MAILCAT_LAYA_ONNX_PATH`, and re-run `mailcat eval run` to confirm accuracy.
- If ClamAV's memory is too much, set `MAILCAT_CLAMAV_MODE=off`. Attachments are then marked *not scanned*,
  never *clean*.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Account shows "failing" | Accounts → Test connection; the app password may have been revoked |
| Every attachment says "not scanned" | `docker compose logs worker`; freshclam needs outbound 443 on the first run |
| Mail stays in INBOX | Write-back off or in dry-run? Category write-back off? Grace period not over? Mail held after an undo? |
| Nothing is deleted | The account's deletion is off or in dry-run, the run was over the cap, or the mail is protected (see Deletion preview) |
| "verify failed" in Activity | The server state after the action did not match. The mail is re-located by Message-ID at the next sync. Check the provider's web UI |
