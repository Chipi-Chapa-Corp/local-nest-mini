#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd -- "$PROJECT_DIR"
SOURCE_REVISION=08be0b4ccbac3e13e374e86fbfead4b4cac343e2
if [[ ! -d models/omnivoice-src/.git ]]; then
  git clone https://github.com/k2-fsa/OmniVoice.git models/omnivoice-src
fi
if [[ "$(git -C models/omnivoice-src rev-parse HEAD)" != "$SOURCE_REVISION" ]]; then
  [[ -z "$(git -C models/omnivoice-src status --porcelain)" ]] || {
    echo "OmniVoice source has local changes; refusing to overwrite" >&2; exit 1;
  }
  git -C models/omnivoice-src fetch origin "$SOURCE_REVISION"
  git -C models/omnivoice-src checkout --detach "$SOURCE_REVISION"
fi
[[ -x models/omnivoice-env/bin/python ]] || python3 -m venv models/omnivoice-env
# Existing audition environments may have inherited user/system packages.
# The system service runs as root and must not depend on the developer's site.
python3 - <<'PY'
from pathlib import Path
config = Path('models/omnivoice-env/pyvenv.cfg')
config.write_text(config.read_text().replace('include-system-site-packages = true',
                                           'include-system-site-packages = false'))
PY
export PYTHONNOUSERSITE=1
models/omnivoice-env/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.11.0+cpu' 'torchaudio==2.11.0+cpu'
models/omnivoice-env/bin/python -m pip install 'transformers==5.20.2' accelerate numpy soundfile librosa pydub \
  'audioop-lts; python_version>="3.13"'
models/omnivoice-env/bin/python -I host/link-omnivoice.py
models/omnivoice-env/bin/python - <<'PY'
from pathlib import Path
from huggingface_hub import snapshot_download
from app.omnivoice_tts import MODEL, REVISION
root = Path.cwd()
snapshot_download(MODEL, revision=REVISION, local_dir=str(root/'models/omnivoice'),
    allow_patterns=['*.json','*.safetensors','*.txt','LICENSE*','audio_tokenizer/*.json',
                    'audio_tokenizer/*.safetensors','audio_tokenizer/LICENSE*'])
PY
mkdir -p models/omnivoice-voices
if [[ ! -s models/omnivoice-voices/laylo-en.wav ]]; then
  # Bootstrap a synthetic reference only when the saved voice is absent.
  models/omnivoice-env/bin/python app/omnivoice_tts.py \
    --model-dir models/omnivoice --reference models/omnivoice-voices/laylo-en.wav \
    --bootstrap-reference \
    --text 'We finally made breakfast without burning the toast.' --lang en --steps 32 \
    --output models/omnivoice-voices/laylo-en.wav
fi
models/omnivoice-env/bin/python -I -c 'import numpy, torch, torchaudio; from omnivoice import OmniVoice'
models/omnivoice-env/bin/python -m pip check
echo "OmniVoice CPU ready: English 16 steps, Russian 32; model and voice assets are local."
