#!/usr/bin/env bash
# One-time host preparation on the Ubuntu server. Run as root from the repository directory:
#   sudo ./deploy/setup-host.sh
# Expects MAILCAT_ROOT (on the LUKS volume) and MAILCAT_BACKUP_ROOT (second disk) from .env.
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  echo "copy .env.example to .env and fill it in first" >&2
  exit 1
fi
set -a; source .env; set +a
: "${MAILCAT_ROOT:?}" "${MAILCAT_BACKUP_ROOT:?}"

# LVM volumes are also /dev/mapper devices, so look for a crypt layer under the mount instead.
source_dev="$(findmnt -n -o SOURCE --target "$MAILCAT_ROOT" || true)"
if ! lsblk -sno TYPE "$source_dev" 2>/dev/null | grep -q crypt; then
  echo "WARNING: $MAILCAT_ROOT is not on a dm-crypt (LUKS) device. See docs/operations.md." >&2
  read -r -p "Continue anyway? [y/N] " answer
  [[ "${answer,,}" == "y" ]] || exit 1
fi

# uid 10001 is the "mailcat" user inside the containers; it does not exist on the host, so use chown
# (which takes a numeric id) instead of install -o.
install -d -m 0700 "$MAILCAT_ROOT/data" "$MAILCAT_ROOT/attachments" "$MAILCAT_ROOT/postgres" "$MAILCAT_BACKUP_ROOT"
chown 10001:10001 "$MAILCAT_ROOT/data" "$MAILCAT_ROOT/attachments" "$MAILCAT_BACKUP_ROOT"
chmod 600 .env

echo "Directories ready:"
ls -ld "$MAILCAT_ROOT"/* "$MAILCAT_BACKUP_ROOT"
echo
echo "Next:"
echo "  docker compose build"
echo "  docker compose run --rm worker mailcat init --admin <your-name>"
echo "  docker compose run --rm worker mailcat model download"
echo "  docker compose up -d"
