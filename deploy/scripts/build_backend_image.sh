#!/usr/bin/env bash
set -euo pipefail
IMAGE_NAME="${1:-daily-report-backend:latest}"
cd "$(dirname "$0")/../.."
docker build -f deploy/Dockerfile.backend -t "$IMAGE_NAME" .
echo "Built backend image: $IMAGE_NAME"
