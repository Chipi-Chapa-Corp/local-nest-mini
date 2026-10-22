#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
SERVICE=local-nest-mini.service
UNIT=/etc/systemd/system/$SERVICE

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root, e.g. distrobox-host-exec sudo $0" >&2
  exit 1
fi
systemctl disable --now "$SERVICE" 2>/dev/null || true
rm -f "$UNIT"
systemctl daemon-reload

if [[ -f "$PROJECT_DIR/config.env" ]]; then
  set -a
  . "$PROJECT_DIR/config.env"
  set +a
  if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    changed=0
    for port in 80 443 8099; do
      rule="rule family=ipv4 source address=$NEST_IP port port=$port protocol=tcp accept"
      if firewall-cmd --permanent --query-rich-rule="$rule" >/dev/null 2>&1; then
        firewall-cmd --permanent --remove-rich-rule="$rule" >/dev/null
        changed=1
      fi
    done
    [[ "$changed" == 1 ]] && firewall-cmd --reload >/dev/null
  fi
fi
echo "service removed"
