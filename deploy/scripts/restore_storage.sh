#!/usr/bin/env bash
# Restore a full storage directory (database + uploaded files) while retaining
# the replaced data as a recoverable timestamped directory.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /path/to/old-storage" >&2
  exit 2
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_STORAGE="$(cd "$1" && pwd)"
TARGET_STORAGE="$APP_DIR/storage"
STAMP="$(date +%Y%m%d%H%M%S)"

if [ ! -f "$SOURCE_STORAGE/daily_report.sqlite3" ]; then
  echo "The source must be a complete storage directory containing daily_report.sqlite3." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  RUNNING="$(cd "$APP_DIR" && docker compose ps -q)"
elif command -v docker-compose >/dev/null 2>&1; then
  RUNNING="$(cd "$APP_DIR" && docker-compose ps -q)"
else
  echo "Docker Compose is required (docker compose or docker-compose)." >&2
  exit 1
fi
if [ -n "$RUNNING" ]; then
  echo "Services are running. Run bash deploy/scripts/stop.sh before restoring data." >&2
  exit 1
fi

if [ -e "$TARGET_STORAGE" ]; then
  mv "$TARGET_STORAGE" "$APP_DIR/storage.before-restore-$STAMP"
fi
mkdir -p "$TARGET_STORAGE"
cp -a "$SOURCE_STORAGE/." "$TARGET_STORAGE/"
echo "Storage restored. Previous storage is at $APP_DIR/storage.before-restore-$STAMP"
echo "Start services with: bash deploy/scripts/start.sh"
