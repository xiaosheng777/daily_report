#!/usr/bin/env bash
# Install an extracted offline bundle. Existing config/, storage/, and source
# files are deliberately preserved unless --refresh-app is requested.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${1:-/opt/daily-report}"
REFRESH_APP=0

if [ "${2:-}" = "--refresh-app" ]; then
  REFRESH_APP=1
elif [ -n "${2:-}" ]; then
  echo "Usage: $0 [install-dir] [--refresh-app]" >&2
  exit 2
fi

if [ ! -f "$SOURCE_DIR/images/daily-report-images.tar" ] || [ ! -f "$SOURCE_DIR/docker-compose.yml" ]; then
  echo "Run this script from the extracted offline bundle directory." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required on the target server but was not found." >&2
  exit 1
fi

if [ "$SOURCE_DIR" = "$(cd "$APP_DIR" 2>/dev/null && pwd || true)" ]; then
  echo "The bundle directory and install directory must be different." >&2
  exit 1
fi

install -d "$APP_DIR" "$APP_DIR/config" "$APP_DIR/storage"

copy_app_files() {
  for item in backend frontend wheelhouse deploy docker-compose.yml DEPLOY.md OFFLINE_UPGRADE_GUIDE.md README.md MANIFEST.txt; do
    [ -e "$SOURCE_DIR/$item" ] || continue
    if [ -e "$APP_DIR/$item" ] && [ "$REFRESH_APP" -ne 1 ]; then
      echo "Keeping existing $APP_DIR/$item"
      continue
    fi
    if [ -d "$SOURCE_DIR/$item" ]; then
      install -d "$APP_DIR/$item"
      cp -a "$SOURCE_DIR/$item/." "$APP_DIR/$item/"
    else
      cp -a "$SOURCE_DIR/$item" "$APP_DIR/$item"
    fi
  done
}

copy_app_files

if [ ! -f "$APP_DIR/config/config.yaml" ]; then
  cp "$SOURCE_DIR/config/config.yaml.example" "$APP_DIR/config/config.yaml"
  echo "Created $APP_DIR/config/config.yaml from the example. Edit it before starting if needed."
else
  echo "Keeping existing $APP_DIR/config/config.yaml"
fi

if [ ! -e "$APP_DIR/config/llm_api_key" ]; then
  install -m 600 /dev/null "$APP_DIR/config/llm_api_key"
  echo "Created empty $APP_DIR/config/llm_api_key"
fi

echo "Loading offline Docker images (no network access is used)..."
docker load -i "$SOURCE_DIR/images/daily-report-images.tar"

echo "Installed to $APP_DIR"
echo "Next: cd $APP_DIR && bash deploy/scripts/start.sh"
