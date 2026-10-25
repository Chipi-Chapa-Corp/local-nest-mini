#!/usr/bin/env bash
# Optional GPU path for OmniVoice: build omnivoice.cpp with Vulkan, check the
# GGUF weights, and pre-encode the voice reference. The CPU path from
# host/setup-omnivoice.sh stays required: the service falls back to it when
# the GPU has too little free VRAM or fails.
#
# Build requirements (on Silverblue, run this inside the dev toolbox):
#   cmake, a C++ compiler, glslc, vulkan-headers/vulkan-loader-devel,
#   spirv-headers-devel. The resulting binary runs on the host with its Mesa
#   Vulkan driver.
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd -- "$PROJECT_DIR"
SOURCE_REVISION=1a468d8b9d65b3544ca1004d2d7c05dfc65d1f3e
SRC=models/omnivoice-cpp-src
GGUF=models/omnivoice-cpp-gguf
BUILD="$SRC/build-vulkan"

if [[ ! -d "$SRC/.git" ]]; then
  git clone https://github.com/ServeurpersoCom/omnivoice.cpp.git "$SRC"
fi
if [[ "$(git -C "$SRC" rev-parse HEAD)" != "$SOURCE_REVISION" ]]; then
  [[ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]] || {
    echo "omnivoice.cpp source has local changes; refusing to overwrite" >&2; exit 1;
  }
  git -C "$SRC" fetch origin "$SOURCE_REVISION"
  git -C "$SRC" checkout --detach "$SOURCE_REVISION"
fi
git -C "$SRC" submodule update --init --recursive

cmake_bin="$(command -v cmake || true)"
if [[ -z "$cmake_bin" ]]; then
  cmake_bin="$(ls models/audio-cpp-tools/lib/python3.*/site-packages/cmake/data/bin/cmake 2>/dev/null | head -1 || true)"
fi
[[ -n "$cmake_bin" ]] || { echo "cmake is required (e.g. dnf install cmake)" >&2; exit 1; }
command -v glslc >/dev/null || { echo "glslc is required (dnf install glslc)" >&2; exit 1; }

"$cmake_bin" -S "$SRC" -B "$BUILD" -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON
"$cmake_bin" --build "$BUILD" --config Release -j "$(nproc)"

for file in omnivoice-base-Q8_0.gguf omnivoice-tokenizer-F32.gguf; do
  [[ -s "$GGUF/$file" ]] || {
    cat >&2 <<EOF
missing $GGUF/$file
Create it from the pinned model in models/omnivoice:
  ln -sfn ../../omnivoice $SRC/checkpoints/OmniVoice   # convert.py reads checkpoints/OmniVoice
  (cd $SRC && python3 convert.py && ./quantize.sh)       # needs the gguf and numpy Python packages
  mkdir -p $GGUF && mv $SRC/models/omnivoice-*.gguf $GGUF/
EOF
    exit 1
  }
done

reference=models/omnivoice-voices/laylo-en.wav
[[ -s "$reference" ]] || { echo "missing $reference; run host/setup-omnivoice.sh first" >&2; exit 1; }
"$BUILD/omnivoice-codec" --model "$GGUF/omnivoice-tokenizer-F32.gguf" -i "$reference"

# Smoke test on the GPU the service will use.
reference_text="$(mktemp)"
output="$(mktemp --suffix=.wav)"
trap 'rm -f "$reference_text" "$output"' EXIT
printf '%s' 'We finally made breakfast without burning the toast.' > "$reference_text"
echo 'Проверка видеокарты.' | "$BUILD/omnivoice-tts" --device "${OMNIVOICE_GPU_DEVICE:-Vulkan0}" \
  --model "$GGUF/omnivoice-base-Q8_0.gguf" --codec "$GGUF/omnivoice-tokenizer-F32.gguf" \
  --lang ru --ref-rvq "${reference%.wav}.rvq" --ref-text "$reference_text" --steps 24 -o "$output" \
  2>&1 | grep -E 'ggml_vulkan: 0 =|\[Perf\] Total'
[[ -s "$output" ]] || { echo "GPU smoke test produced no audio" >&2; exit 1; }
echo "OmniVoice GPU ready: Q8_0, 24 steps, used when more than 1.7 GiB VRAM is free."
