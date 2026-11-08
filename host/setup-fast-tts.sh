#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd -- "$PROJECT_DIR"
[[ -x models/fast-tts-env/bin/python ]] || python3 -m venv models/fast-tts-env
export PYTHONNOUSERSITE=1
models/fast-tts-env/bin/python -m pip install -r requirements-fast-tts.txt
models/fast-tts-env/bin/python host/download-fast-tts.py
models/fast-tts-env/bin/python -I app/fast_tts_worker.py --assets models/fast-tts --check
echo 'Silero v5.6 / Xenia and Pocket TTS 2M / Alba ready offline.'
