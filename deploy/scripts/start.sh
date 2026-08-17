#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$APP_DIR"

if [ ! -f config/config.yaml ]; then
  echo "Missing $APP_DIR/config/config.yaml. Run install.sh first." >&2
  exit 1
fi
if [ ! -e config/llm_api_key ]; then
  install -m 600 /dev/null config/llm_api_key
fi

if docker compose version >/dev/null 2>&1; then
  docker compose up -d --remove-orphans
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose up -d --remove-orphans
else
  echo "Docker Compose is required (docker compose or docker-compose)." >&2
  exit 1
fi

echo "Daily Report is starting. Check status with: bash deploy/scripts/status.sh"
