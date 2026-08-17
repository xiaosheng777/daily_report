#!/usr/bin/env bash
# Take a consistent, portable backup of the SQLite database and all files it
# references. Stop the application first so SQLite WAL files are not changing.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_DIR="${1:-$APP_DIR/backups}"
STAMP="$(date +%Y%m%d%H%M%S)"

if [ ! -d "$APP_DIR/storage" ]; then
  echo "Storage directory does not exist: $APP_DIR/storage" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  if [ -n "$(cd "$APP_DIR" && docker compose ps -q)" ]; then
    echo "Services are running. Run bash deploy/scripts/stop.sh before making a backup." >&2
    exit 1
  fi
elif command -v docker-compose >/dev/null 2>&1; then
  if [ -n "$(cd "$APP_DIR" && docker-compose ps -q)" ]; then
    echo "Services are running. Run bash deploy/scripts/stop.sh before making a backup." >&2
    exit 1
  fi
fi

mkdir -p "$BACKUP_DIR"
tar -C "$APP_DIR" -czf "$BACKUP_DIR/daily-report-storage-$STAMP.tar.gz" storage
echo "Backup created: $BACKUP_DIR/daily-report-storage-$STAMP.tar.gz"
