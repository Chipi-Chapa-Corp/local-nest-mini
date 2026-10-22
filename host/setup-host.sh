#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd -- "$PROJECT_DIR"

if [[ ! -x .venv/bin/mitmdump ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import mitmproxy, pychromecast' >/dev/null 2>&1; then
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

if [[ ! -s proxy-config/mitmproxy-ca.pem ]]; then
  (umask 077; .venv/bin/python -c 'import sys; from mitmproxy import certs; certs.CertStore.from_store(sys.argv[1], "mitmproxy", 2048)' proxy-config)
fi
chmod 600 proxy-config/mitmproxy-ca.pem 2>/dev/null || true
mkdir -p data/streams data/tts data/logs
echo "host dependencies ready in $PROJECT_DIR/.venv"
