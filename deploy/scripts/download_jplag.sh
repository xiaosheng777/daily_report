#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:-6.0.0}"
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT_DIR/vendor/jplag/jplag.jar"
URL="https://github.com/jplag/JPlag/releases/download/v${VERSION}/jplag-${VERSION}-jar-with-dependencies.jar"
mkdir -p "$(dirname "$OUT")"
curl -L --fail -o "$OUT" "$URL"
java -version
java -jar "$OUT" --help >/dev/null
echo "Downloaded and verified: $OUT"
