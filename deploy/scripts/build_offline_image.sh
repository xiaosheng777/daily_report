#!/usr/bin/env bash
set -euo pipefail
IMAGE_NAME="${1:-daily-report-backend:latest}"
OUT_FILE="${2:-daily-report-backend-image.tar}"
cd "$(dirname "$0")/../.."
docker build -f deploy/Dockerfile.backend -t "$IMAGE_NAME" .
docker save "$IMAGE_NAME" -o "$OUT_FILE"
echo "Saved backend image to $OUT_FILE"
echo "Note: this tar export is optional. For manual-image deployment, use build_backend_image.sh instead."
