#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd -- "$PROJECT_DIR"

if [[ "${1:-2}" == mini ]]; then
  python3 -m venv models/kitten-mini-env
  # The upstream ONNX-only installation deliberately omits the Kitten 2/Torch stack.
  models/kitten-mini-env/bin/python -m pip install --no-cache-dir --no-deps kittenml==0.9.6
  models/kitten-mini-env/bin/python -m pip install --no-cache-dir \
    onnxruntime==1.31.0 espeakng_loader==0.2.4 phonemizer==3.4.0 \
    soundfile==0.14.0 numpy==2.5.4 huggingface_hub==2.3.0
  models/kitten-mini-env/bin/python - <<'PY'
from pathlib import Path
from urllib.request import urlopen
from app.kitten_tts import MINI_MODEL, MINI_REVISION
root = Path('models/kitten-tts-mini-0.8')
root.mkdir(parents=True, exist_ok=True)
for name in ('config.json', 'kitten_tts_mini_v0_8.onnx', 'voices.npz'):
    target = root / name
    temporary = target.with_suffix(target.suffix + '.part')
    with urlopen(f'https://huggingface.co/{MINI_MODEL}/resolve/{MINI_REVISION}/{name}', timeout=120) as source, temporary.open('wb') as dest:
        while block := source.read(1024 * 1024):
            dest.write(block)
    temporary.replace(target)
(root / 'revision').write_text(MINI_REVISION + '\n')
PY
  mkdir -p data/benchmarks/kitten-tts-mini
  models/kitten-mini-env/bin/python app/kitten_tts.py --family mini \
    --cache-dir "$PROJECT_DIR/models/kitten-tts-mini-0.8" \
    --text "Ready for a local voice test." \
    --output "$PROJECT_DIR/data/benchmarks/kitten-tts-mini/setup.wav"
  echo "Mini ready. Select TTS_BACKEND=kitten-mini, then ./run.sh restart. Russian uses Pocket TTS."
  exit
fi
if [[ "${1:-2}" != 2 ]]; then
  echo "usage: $0 [2|mini]" >&2
  exit 2
fi

# Keep CPU LibTorch separate from the proxy/assistant's Python dependencies.
python3 -m venv models/kitten-env
models/kitten-env/bin/python -m pip install --no-cache-dir \
  torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu
models/kitten-env/bin/python -m pip install --no-cache-dir kitten-tts-cpp==0.2.0
mkdir -p data/benchmarks/kitten-tts-2
models/kitten-env/bin/python app/kitten_tts.py \
  --cache-dir "$PROJECT_DIR/models/kitten-tts-2" --download \
  --decoder "${KITTEN_DECODER:-default}" \
  --text "Ready for a local voice test." \
  --output "$PROJECT_DIR/data/benchmarks/kitten-tts-2/setup.wav"
echo "KittenTTS 2 ready. Select TTS_BACKEND=kitten in config.env, then ./run.sh restart."
