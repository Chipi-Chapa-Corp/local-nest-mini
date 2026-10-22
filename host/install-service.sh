#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
SERVICE=local-nest-mini.service
UNIT=/etc/systemd/system/$SERVICE

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root, e.g. distrobox-host-exec sudo $0" >&2
  exit 1
fi
[[ -f "$PROJECT_DIR/config.env" ]] || { echo "missing config.env" >&2; exit 1; }
set -a
. "$PROJECT_DIR/config.env"
set +a
: "${NEST_IP:?missing NEST_IP}"

if ! "$PROJECT_DIR/.venv/bin/python" -c 'import mitmproxy, pychromecast' >/dev/null 2>&1; then
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != root ]]; then
    sudo -u "$SUDO_USER" "$PROJECT_DIR/host/setup-host.sh"
  else
    "$PROJECT_DIR/host/setup-host.sh"
  fi
fi

chmod 600 "$PROJECT_DIR/proxy-config/mitmproxy-ca.pem"
install -m 0644 "$PROJECT_DIR/host/$SERVICE" "$UNIT"

if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  changed=0
  for port in 80 443 8099; do
    rule="rule family=ipv4 source address=$NEST_IP port port=$port protocol=tcp accept"
    if ! firewall-cmd --permanent --query-rich-rule="$rule" >/dev/null 2>&1; then
      firewall-cmd --permanent --add-rich-rule="$rule" >/dev/null
      changed=1
      echo "added firewall rule for $NEST_IP tcp/$port"
    fi
  done
  if [[ "$changed" == 1 ]]; then
    firewall-cmd --reload >/dev/null
  fi
else
  echo "firewalld not active; ensure TCP 80/443/8099 from $NEST_IP is allowed" >&2
fi

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl reset-failed "$SERVICE" 2>/dev/null || true
systemctl restart "$SERVICE"
systemctl --no-pager --full status "$SERVICE" || true
echo
echo "logs: sudo journalctl -u $SERVICE -f"
