#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd -- "$PROJECT_DIR"
umask 077
case "${1:-}" in
  ''|--check) ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

[[ -f config.env ]] || { echo "missing config.env; copy config.env.example and configure your network" >&2; exit 1; }
set -a
. ./config.env
if [[ -f .env ]]; then
  . ./.env
fi
set +a
for var in NEST_IP NEST_MAC PC_IP GATEWAY INTERFACE; do
  : "${!var:?missing $var in config.env}"
done
: "${SERVICE_DURATION:=0}"

[[ -s proxy-config/mitmproxy-ca.pem ]] || { echo "missing proxy-config/mitmproxy-ca.pem" >&2; exit 1; }
[[ -x .venv/bin/python ]] || { echo "missing .venv/bin/python; run bash host/setup-host.sh" >&2; exit 1; }
.venv/bin/python -I -c 'import mitmproxy, pychromecast' || {
  echo "proxy dependencies unavailable; run bash host/setup-host.sh" >&2; exit 1;
}
chmod 600 proxy-config/mitmproxy-ca.pem

export DATA_DIR="$PROJECT_DIR/data"
export TTS_DIR="$PROJECT_DIR/data/tts"
export PROXY_CONFIG="$PROJECT_DIR/proxy-config"
export STT_MODEL="$PROJECT_DIR/models/stt/futo.bin"
export WHISPER_BIN="$PROJECT_DIR/bin/whisper-cli"
export POCKET_BIN="$PROJECT_DIR/bin/pocket-tts"
export TTS_BACKEND="${TTS_BACKEND:-omnivoice}"
export RU_CONFIG="$PROJECT_DIR/models/ru/config.yaml"
export RU_VOICE="$PROJECT_DIR/models/ru/voice.safetensors"
export LISTEN_HOST=0.0.0.0
export LISTEN_PORT=8099
export ASSISTANT_URL="${ASSISTANT_URL:-http://127.0.0.1:8099}"
export ASSISTANT_ENABLED="${ASSISTANT_ENABLED:-1}"
export PLAYER_ENABLED="${PLAYER_ENABLED:-1}"
export OUTPUT_MODE="${OUTPUT_MODE:-native}"
export ALLOW_GOOGLE_UPSTREAM="${ALLOW_GOOGLE_UPSTREAM:-0}"
export NATIVE_TEMPLATE="${NATIVE_TEMPLATE:-$PROJECT_DIR/data/native-template.bin}"
export NATIVE_BITRATE="${NATIVE_BITRATE:-32k}"
export CAST_NAME="${CAST_NAME:-Office speaker}"
export MONITOR_ENABLED="${MONITOR_ENABLED:-1}"
export MONITOR_HOST="${MONITOR_HOST:-127.0.0.1}"
export MONITOR_PORT="${MONITOR_PORT:-53412}"

if [[ "$ASSISTANT_ENABLED" == 1 ]]; then
  [[ -x "$WHISPER_BIN" ]] || { echo "missing Whisper binary" >&2; exit 1; }
  if [[ "$TTS_BACKEND" == pocket ]]; then
    [[ -x "$POCKET_BIN" ]] || { echo "missing Pocket TTS binary" >&2; exit 1; }
  fi
  if [[ "$TTS_BACKEND" == omnivoice ]]; then
    omnivoice_python="${OMNIVOICE_PYTHON:-$PROJECT_DIR/models/omnivoice-env/bin/python}"
    [[ -x "$omnivoice_python" ]] || {
      echo "missing OmniVoice environment; run bash host/setup-omnivoice.sh" >&2; exit 1;
    }
    if ! "$omnivoice_python" -I -c 'import numpy, torch, torchaudio; from omnivoice import OmniVoice'; then
      echo "OmniVoice dependencies are missing for the service account; run bash host/setup-omnivoice.sh" >&2
      exit 1
    fi
    # The GPU path is optional: without it every reply uses the CPU.
    gpu_bin="${OMNIVOICE_GPU_BIN:-$PROJECT_DIR/models/omnivoice-cpp-src/build-vulkan/omnivoice-tts}"
    if [[ "${OMNIVOICE_GPU:-auto}" == off ]]; then
      echo "OmniVoice GPU disabled; replies use the CPU"
    elif [[ -x "$gpu_bin" ]] && grep -q Usage < <("$gpu_bin" --help 2>&1); then
      echo "OmniVoice GPU available; used when more than ${OMNIVOICE_GPU_MIN_FREE_MIB:-1740} MiB VRAM is free"
    else
      echo "OmniVoice GPU build missing or not runnable; replies use the CPU (run host/setup-omnivoice-vulkan.sh)"
    fi
  fi
  [[ -s "$STT_MODEL" ]] || { echo "missing STT model: $STT_MODEL" >&2; exit 1; }
  "$WHISPER_BIN" --help >/dev/null 2>&1 || {
    echo "Whisper cannot start; check its shared-library dependencies" >&2; exit 1;
  }
  command -v ffmpeg >/dev/null || { echo "missing ffmpeg; install it with your host package manager" >&2; exit 1; }
  if [[ "$OUTPUT_MODE" == native ]]; then
    for asset in "$NATIVE_TEMPLATE" "${NATIVE_NOSPEECH_TEMPLATE:-$DATA_DIR/native-nospeech.bin}"; do
      [[ -s "$asset" ]] || { echo "missing owned-device capture: $asset (see README.md Setup)" >&2; exit 1; }
    done
  fi
fi
if [[ "${OFFLINE_BOOTSTRAP_ENABLED:-1}" == 1 ]]; then
  DATA_DIR="$DATA_DIR" .venv/bin/python -c \
    'import os; from pathlib import Path; from app.bootstrap import load_token_response; load_token_response(Path(os.environ["DATA_DIR"]))' || {
    echo "missing or invalid data/offline-account-ids.json; supply linked account IDs from your own Nest (see README.md Setup)" >&2
    exit 1
  }
fi

if [[ "${1:-}" == --check ]]; then
  echo "host runtime prerequisites ready in $PROJECT_DIR"
  exit
fi

for port in 8099 18444 18445 18081 53412; do
  if timeout 1 bash -c "</dev/tcp/127.0.0.1/$port" 2>/dev/null; then
    echo "127.0.0.1:$port is already in use; refusing to start" >&2
    exit 1
  fi
done

mkdir -p data/streams data/tts data/logs
# Keep owned-device captures private but readable by the project owner.
for capture in data/streams/*-{req,resp}.bin; do
  [[ -f "$capture" && ! -L "$capture" ]] || continue
  chown --reference="$PROJECT_DIR" "$capture"
  chmod 600 "$capture"
done
rm -f network-helper.stop

assistant_pid=""
proxy_pid=""
helper_pid=""
s3_pid=""
cleanup() {
  touch network-helper.stop
  for pid in "$s3_pid" "$assistant_pid" "$helper_pid"; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$proxy_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -9 "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ "$ASSISTANT_ENABLED" == 1 ]]; then
  .venv/bin/python -m app.server &
  assistant_pid=$!
  for _ in $(seq 1 80); do
    if timeout 1 bash -c '</dev/tcp/127.0.0.1/8099' 2>/dev/null; then
      break
    fi
    if ! kill -0 "$assistant_pid" 2>/dev/null; then
      echo "assistant server exited during startup" >&2
      exit 1
    fi
    sleep 0.25
  done
  if ! timeout 1 bash -c '</dev/tcp/127.0.0.1/8099' 2>/dev/null; then
    echo "assistant server did not open 8099" >&2
    exit 1
  fi
  if ! timeout 5 .venv/bin/python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8099/health", timeout=3)' >/dev/null 2>&1; then
    echo "assistant health check failed" >&2
    exit 1
  fi
  echo "assistant pid $assistant_pid"
fi

if [[ "$ASSISTANT_ENABLED" == 1 && "$OUTPUT_MODE" == native ]]; then
  if [[ "$ALLOW_GOOGLE_UPSTREAM" == 1 ]]; then
    .venv/bin/python -m app.s3_relay &
  else
    .venv/bin/python -m app.s3_server &
  fi
  s3_pid=$!
  for _ in $(seq 1 80); do
    if timeout 1 bash -c '</dev/tcp/127.0.0.1/18444' 2>/dev/null; then
      break
    fi
    kill -0 "$s3_pid" 2>/dev/null || { echo "local S3 server exited during startup" >&2; exit 1; }
    sleep 0.25
  done
  timeout 1 bash -c '</dev/tcp/127.0.0.1/18444' 2>/dev/null || {
    echo "local S3 server did not open 18444" >&2; exit 1;
  }
fi

./start-proxy.sh &
proxy_pid=$!

for _ in $(seq 1 120); do
  if timeout 1 bash -c '</dev/tcp/127.0.0.1/18445' 2>/dev/null &&
     timeout 1 bash -c '</dev/tcp/127.0.0.1/18081' 2>/dev/null; then
    break
  fi
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    echo "proxy exited during startup" >&2
    exit 1
  fi
  sleep 0.25
done
if ! timeout 1 bash -c '</dev/tcp/127.0.0.1/18445' 2>/dev/null; then
  echo "proxy did not open 18445" >&2
  exit 1
fi

echo "proxy pid $proxy_pid; data in $PROJECT_DIR/data"
echo "starting helper for $NEST_IP ($NEST_MAC); service duration ${SERVICE_DURATION}s (0 means until stopped)"
python3 "$PROJECT_DIR/nest_network_helper.py" \
  --interface "$INTERFACE" \
  --nest "$NEST_IP" \
  --nest-mac "$NEST_MAC" \
  --pc "$PC_IP" \
  --gateway "$GATEWAY" \
  --duration "$SERVICE_DURATION" \
  --shared-http &
helper_pid=$!

pids=("$proxy_pid" "$helper_pid")
if [[ -n "$assistant_pid" ]]; then
  pids+=("$assistant_pid")
fi
if [[ -n "$s3_pid" ]]; then
  pids+=("$s3_pid")
fi
set +e
wait -n "${pids[@]}"
status=$?
set -e
echo "a service child exited with status $status; shutting down" >&2
exit "$status"
