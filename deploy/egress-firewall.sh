#!/usr/bin/env bash
# Limit what the mailcat containers can reach on the internet: IMAP over TLS (993), HTTPS (443,
# blocklist feeds, ClamAV signatures, Microsoft OAuth, Discord alerts) and DNS. Everything else
# from the egress network is dropped. Tailscale and the host are unaffected.
#
#   sudo ./deploy/egress-firewall.sh          # apply (idempotent)
#   sudo ./deploy/egress-firewall.sh remove   # undo
#
# To make it permanent, run it from a systemd unit after docker.service (docs/operations.md).
set -euo pipefail

SUBNET="${MAILCAT_EGRESS_SUBNET:-172.31.250.0/24}"
CHAIN="MAILCAT-EGRESS"
EXTRA_TCP_PORTS="${MAILCAT_EXTRA_TCP_PORTS:-}"   # e.g. "143 587" for an unusual custom-domain host

remove() {
  iptables -D DOCKER-USER -s "$SUBNET" -j "$CHAIN" 2>/dev/null || true
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
}

if [[ "${1:-}" == "remove" ]]; then
  remove
  echo "egress rules removed"
  exit 0
fi

remove
iptables -N "$CHAIN"
# replies to connections that came in (dashboard via published port)
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# traffic between containers on the same network
iptables -A "$CHAIN" -d "$SUBNET" -j RETURN
iptables -A "$CHAIN" -p udp --dport 53 -j RETURN
iptables -A "$CHAIN" -p tcp --dport 53 -j RETURN
iptables -A "$CHAIN" -p tcp --dport 993 -j RETURN
iptables -A "$CHAIN" -p tcp --dport 443 -j RETURN
for port in $EXTRA_TCP_PORTS; do
  iptables -A "$CHAIN" -p tcp --dport "$port" -j RETURN
done
iptables -A "$CHAIN" -j LOG --log-prefix "mailcat-egress-drop " --log-level 4 -m limit --limit 6/min
iptables -A "$CHAIN" -j DROP
iptables -I DOCKER-USER -s "$SUBNET" -j "$CHAIN"
echo "egress from $SUBNET limited to DNS, 443, 993${EXTRA_TCP_PORTS:+, $EXTRA_TCP_PORTS}"
