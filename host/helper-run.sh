#!/usr/bin/env bash
# The privileged host component uses only Python's standard library and nft/ip.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$project_dir"
set -a
. ./config.env
set +a
for var in NEST_MAC PC_IP GATEWAY INTERFACE; do
  : "${!var:?missing $var in config.env}"
done
# Wait for Compose on user-manager startup and after container updates.
python3 - <<'PY'
import socket, time
end = time.monotonic() + 120
while True:
    try:
        for port in (8099, 18444, 18445, 18081):
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass
        break
    except OSError:
        if time.monotonic() >= end:
            raise SystemExit('Nest containers not ready; start local-infra Compose first')
        time.sleep(1)
PY
exec python3 "$project_dir/nest_network_helper.py" \
  --interface "$INTERFACE" --nest "${NEST_IP:-}" --nest-mac "$NEST_MAC" \
  --pc "$PC_IP" --gateway "$GATEWAY" --duration 0 --shared-http
