#!/usr/bin/env bash
# One-time server preparation. Run as root from the repository directory:
#   sudo ./deploy/prepare-server.sh --usb /dev/sdX [--size 20G]
#
# 1. Formats the USB disk as ext4 for backups and mounts it at /mnt/backup on every boot.
# 2. Creates an encrypted LUKS file container for all mail data and mounts it at /srv/mailcat.
# 3. Writes .env: generates the database password and credential key, asks for the backup passphrase.
# 4. Creates the data directories (deploy/setup-host.sh).
# 5. Optionally installs Tailscale.
#
# Every secret is typed or generated on this machine only and is never printed.
set -euo pipefail

USB=""
SIZE="20G"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --usb) USB="$2"; shift 2 ;;
    --size) SIZE="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -n "$USB" ]] || { echo "usage: sudo $0 --usb /dev/sdX [--size 20G]" >&2; exit 2; }

cd "$(dirname "$0")/.."
REPO="$(pwd)"
OWNER="$(stat -c %U "$REPO")"
CONTAINER=/var/lib/mailcat/mailcat.luks
MAPPER=mailcat_crypt
DATA=/srv/mailcat
BACKUP_MNT=/mnt/backup
step() { printf '\n== %s\n' "$*"; }

# --- 1. backup disk ------------------------------------------------------------------------
step "Backup disk $USB"
if [[ "$(lsblk -dno TRAN "$USB" 2>/dev/null)" != "usb" ]]; then
  echo "$USB is not a USB disk; refusing to format it" >&2
  exit 1
fi
if findmnt -no FSTYPE "$BACKUP_MNT" 2>/dev/null | grep -q ext4 && [[ "$(lsblk -no LABEL "$USB" | head -1)" == "mailcat-backup" ]]; then
  echo "already prepared and mounted at $BACKUP_MNT"
else
  lsblk -o NAME,TRAN,FSTYPE,LABEL,SIZE,MODEL,MOUNTPOINTS "$USB"
  echo
  echo "Everything on $USB will be ERASED."
  read -r -p "Type ERASE to continue: " answer
  [[ "$answer" == "ERASE" ]] || { echo "aborted"; exit 1; }
  for part in $(lsblk -lnpo NAME "$USB"); do
    umount "$part" 2>/dev/null || true
  done
  wipefs -a "$USB" >/dev/null
  mkfs.ext4 -q -F -L mailcat-backup "$USB"
  mkdir -p "$BACKUP_MNT"
  sed -i '/[[:space:]]\/mnt\/backup[[:space:]]/d' /etc/fstab
  echo "LABEL=mailcat-backup $BACKUP_MNT ext4 defaults,noexec,nosuid,nodev,nofail 0 2" >> /etc/fstab
  systemctl daemon-reload
  mount "$BACKUP_MNT"
  echo "formatted and mounted at $BACKUP_MNT"
fi

# --- 2. encrypted data container ----------------------------------------------------------
step "Encrypted storage ($SIZE file container)"
if [[ ! -f "$CONTAINER" ]]; then
  install -d -m 0700 "$(dirname "$CONTAINER")"
  fallocate -l "$SIZE" "$CONTAINER"
  chmod 600 "$CONTAINER"
  echo "Choose the storage passphrase. Keep it in your password manager: it is needed after every reboot."
  cryptsetup luksFormat --type luks2 --batch-mode --verify-passphrase "$CONTAINER"
  cryptsetup open "$CONTAINER" "$MAPPER"
  mkfs.ext4 -q -L mailcat "/dev/mapper/$MAPPER"
else
  echo "container exists"
  [[ -e "/dev/mapper/$MAPPER" ]] || cryptsetup open "$CONTAINER" "$MAPPER"
fi
if ! findmnt -n "$DATA" >/dev/null; then
  mkdir -p "$DATA"
  # While locked, the empty mount point is immutable: nothing (Postgres included) can write mail
  # data to the unencrypted disk by mistake, so the stack fails to start instead.
  chattr +i "$DATA"
  mount "/dev/mapper/$MAPPER" "$DATA"
fi
echo "mounted at $DATA"

# --- 3. .env ---------------------------------------------------------------------------------
step ".env"
if [[ -f .env ]]; then
  echo ".env exists; leaving it as it is"
else
  while true; do
    read -r -s -p "Backup passphrase (16+ characters, no single quotes; store it off this server): " p1; echo
    read -r -s -p "Repeat: " p2; echo
    if [[ "$p1" != "$p2" ]]; then echo "they differ"; continue; fi
    if [[ ${#p1} -lt 16 || "$p1" == *"'"* ]]; then echo "too short or contains a single quote"; continue; fi
    break
  done
  read -r -p "Discord webhook URL for alerts (optional, Enter to skip): " webhook
  echo "generating the database password and the credential encryption key..."
  key="$(docker run --rm mailcat-api:latest mailcat gen-key)"
  dbpass="$(openssl rand -hex 32)"
  BACKUP_PASSPHRASE="$p1" WEBHOOK="$webhook" SECRET_KEY="$key" DB_PASSWORD="$dbpass" python3 - <<'PY'
import os, re
values = {
    "POSTGRES_PASSWORD": os.environ["DB_PASSWORD"],
    "MAILCAT_SECRET_KEY": os.environ["SECRET_KEY"],
    "MAILCAT_BACKUP_PASSPHRASE": os.environ["BACKUP_PASSPHRASE"],
    "MAILCAT_DISCORD_WEBHOOK_URL": os.environ["WEBHOOK"],
    "MAILCAT_ROOT": "/srv/mailcat",
    "MAILCAT_BACKUP_ROOT": "/mnt/backup/mailcat",
    "MAILCAT_READ_ONLY": "true",
}
lines = open(".env.example", encoding="utf-8").read().splitlines()
out = []
for line in lines:
    m = re.match(r"^([A-Z0-9_]+)=", line)
    if m and m.group(1) in values:
        line = f"{m.group(1)}='{values[m.group(1)]}'"
    out.append(line)
fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
PY
  unset p1 p2 key dbpass
  echo "written (mode 600). The credential key is inside .env: keep a copy in your password manager."
fi
chown "$OWNER:$OWNER" .env
chmod 600 .env

# --- 4. directories --------------------------------------------------------------------------
step "Data directories"
mkdir -p "$BACKUP_MNT/mailcat"
./deploy/setup-host.sh

# --- 5. Tailscale ----------------------------------------------------------------------------
step "Tailscale"
if command -v tailscale >/dev/null; then
  echo "already installed"
else
  read -r -p "Install Tailscale now? [y/N] " answer
  if [[ "${answer,,}" == "y" ]]; then
    curl -fsSL https://tailscale.com/install.sh | sh
    tailscale up
  else
    echo "skipped; the dashboard stays reachable only on 127.0.0.1 of this server"
  fi
fi

step "Done"
echo "Data:    $DATA (encrypted)   Backups: $BACKUP_MNT/mailcat"
echo "After a reboot, unlock the storage with:  sudo ./deploy/unlock-storage.sh"
