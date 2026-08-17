#!/usr/bin/env bash
# Stops containers but never removes volumes or the host-mounted storage data.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$APP_DIR"

if docker compose version >/dev/null 2>&1; then
  docker compose stop
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose stop
else
  echo "Docker Compose is required (docker compose or docker-compose)." >&2
  exit 1
fi
