#!/usr/bin/env bash
# After a reboot: unlock the encrypted mail storage and start the stack.
#   sudo ./deploy/unlock-storage.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")/.."

[[ -e /dev/mapper/mailcat_crypt ]] || cryptsetup open /var/lib/mailcat/mailcat.luks mailcat_crypt
findmnt -n /srv/mailcat >/dev/null || mount /dev/mapper/mailcat_crypt /srv/mailcat
echo "storage unlocked at /srv/mailcat"
sudo -u "$(stat -c %U .)" docker compose up -d
