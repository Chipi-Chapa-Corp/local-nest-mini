#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
: "${DATA_DIR:=$SCRIPT_DIR/data}"
: "${PROXY_CONFIG:=$SCRIPT_DIR/proxy-config}"
export DATA_DIR PROXY_CONFIG
umask 077
mkdir -p "$DATA_DIR/streams" "$DATA_DIR/tts" "$DATA_DIR/logs"

if [[ -x "$SCRIPT_DIR/.venv/bin/mitmdump" ]]; then
  MITMDUMP="$SCRIPT_DIR/.venv/bin/mitmdump"
else
  MITMDUMP="$(command -v mitmdump)"
fi

exec "$MITMDUMP" \
  --mode 'reverse:https://placeholder.invalid@127.0.0.1:18445' \
  --mode 'reverse:http://placeholder.invalid@127.0.0.1:18081' \
  --set "confdir=$PROXY_CONFIG" \
  --set connection_strategy=lazy --emudhra-old --set upstream_cert=false \
  --set keep_host_header=true --set validate_inbound_headers=false --set flow_detail=0 \
  -s "$SCRIPT_DIR/nest_relay.py" -w "$DATA_DIR/nest-traffic.mitm"
