#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p wheelhouse

docker run --rm \
  -v "$PWD:/work" \
  -w /work \
  python:3.12-slim \
  sh -c "python -m pip install -U pip && pip download --only-binary=:all: -r backend/requirements.txt -d wheelhouse"

echo "Prepared offline Python wheels in: wheelhouse/"
ls -lh wheelhouse
