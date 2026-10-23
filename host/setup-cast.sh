#!/usr/bin/env bash
# Cache the public receiver SDK once; the Nest never fetches it upstream.
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
target="$PROJECT_DIR/data/cast"
mkdir -p "$target"
temp="$(mktemp -d "$target/.sdk-XXXXXX")"
trap 'rm -rf -- "$temp"' EXIT
# Public SDK snapshots tested with the local WAV receiver. Fail on changes.
while read -r digest path name; do
  curl --fail --location --proto '=https' --max-time 60 \
    "https://www.gstatic.com/$path" -o "$temp/$name"
  printf '%s  %s\n' "$digest" "$temp/$name" | sha256sum -c -
done <<'ASSETS'
9bac6813258ae43ab6bc515d77c64bea4adb057930d404b835f53f05e430f254 cast/sdk/libs/caf_receiver/v3/cast_receiver_framework.js cast_receiver_framework.js
32f1f045575d9e03d2ca3c5d80d49de04aa48d907455d17c96c715012221d04f external_hosted/cast_3p/js_libs/prod/castlite_js_lib.js castlite_js_lib.js
f4b42997c095b0334b3f7a25c59a9f5ee444c7027bece85f7756b8412f49c85b external_hosted/cast_3p/js_libs/prod/sizzle.js sizzle.js
ASSETS
chmod 644 "$temp/"*.js
mv -- "$temp/"*.js "$target/"
echo 'Local Cast receiver SDK ready.'
