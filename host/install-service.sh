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

if ! systemctl cat local-infra-gateway.service >/dev/null 2>&1; then
  echo 'Install the shared gateway first: sudo bash ~/Projects/Misc/local-infra/scripts/install-gateway.sh' >&2
  exit 1
fi

# Always prepare: imports alone don't detect stale launchers or missing CA files.
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != root ]]; then
  sudo -u "$SUDO_USER" bash "$PROJECT_DIR/host/setup-host.sh"
else
  bash "$PROJECT_DIR/host/setup-host.sh"
fi

bash "$PROJECT_DIR/host/service-run.sh" --check
chmod 600 "$PROJECT_DIR/proxy-config/mitmproxy-ca.pem"
rendered_unit="$(mktemp)"
trap 'rm -f -- "$rendered_unit"' EXIT
python3 "$PROJECT_DIR/host/render-service.py" > "$rendered_unit"
install -m 0644 "$rendered_unit" "$UNIT"

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
