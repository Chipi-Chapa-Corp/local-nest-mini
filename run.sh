#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
cd -- "$PROJECT_DIR"
SERVICE=local-nest-mini.service

action="${1:-install}"
case "$action" in
  install|stop|restart|status|logs|uninstall) ;;
  --help|-h|help)
    echo "usage: $0 [install|stop|restart|status|logs|uninstall]"
    echo "       default: install (install, enable, restart, show status/logs)"
    exit
    ;;
  *)
    echo "unknown action: $action" >&2
    echo "usage: $0 [install|stop|restart|status|logs|uninstall]" >&2
    exit 2
    ;;
esac

[[ -f config.env ]] || { echo "missing config.env" >&2; exit 1; }

host=()
where="host"
if command -v distrobox-host-exec >/dev/null 2>&1; then
  host=(distrobox-host-exec)
  where="host via distrobox"
fi

password=""
trap 'unset password 2>/dev/null || true' EXIT

run_sudo() {
  if [[ -n "$password" ]]; then
    printf '%s\n' "$password" | "${host[@]}" sudo -S -p '' "$@"
  else
    "${host[@]}" sudo -n "$@"
  fi
}

if ! "${host[@]}" sudo -n true >/dev/null 2>&1; then
  if [[ ! -t 0 ]]; then
    echo "host sudo needs a password; run this from an interactive terminal" >&2
    exit 1
  fi
  read -rsp "[sudo] password for $where: " password
  echo
  if ! run_sudo true; then
    echo "sudo authentication failed" >&2
    exit 1
  fi
fi

show_status() {
  run_sudo systemctl --no-pager --full status "$SERVICE" || true
}

show_logs() {
  run_sudo journalctl -u "$SERVICE" -n 50 --no-pager || true
}

case "$action" in
  install)
    echo "installing and starting $SERVICE on $where"
    run_sudo "$PROJECT_DIR/host/install-service.sh"
    echo
    echo "service status:"
    show_status
    echo
    echo "recent logs:"
    show_logs
    echo
    echo "follow logs with:"
    echo "  distrobox-host-exec sudo journalctl -u $SERVICE -f"
    ;;
  stop)
    run_sudo systemctl stop "$SERVICE"
    echo "stopped $SERVICE"
    ;;
  restart)
    run_sudo systemctl reset-failed "$SERVICE" 2>/dev/null || true
    run_sudo systemctl restart "$SERVICE"
    echo "restarted $SERVICE"
    echo
    show_status
    echo
    show_logs
    ;;
  status)
    show_status
    ;;
  logs)
    show_logs
    ;;
  uninstall)
    run_sudo "$PROJECT_DIR/host/uninstall-service.sh"
    ;;
esac
