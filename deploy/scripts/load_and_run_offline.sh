#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${1:-/opt/daily_report}"
BACKEND_IMAGE="${2:-daily-report-backend:latest}"
mkdir -p "$APP_DIR"/{config,storage,frontend,deploy}
if [ ! -f "$APP_DIR/config/config.yaml" ]; then
  echo "Missing $APP_DIR/config/config.yaml"
  exit 1
fi
if [ ! -f "$APP_DIR/config/llm_api_key" ]; then
  echo "Missing $APP_DIR/config/llm_api_key"
  echo "Create it with: printf '%s' 'YOUR_KEY' > $APP_DIR/config/llm_api_key && chmod 600 $APP_DIR/config/llm_api_key"
  exit 1
fi
cd "$APP_DIR"
docker rm -f daily-report-web daily-report-backend >/dev/null 2>&1 || true
docker network create daily-report-net >/dev/null 2>&1 || true

docker run -d \
  --name daily-report-backend \
  --restart always \
  --network daily-report-net \
  -v "$APP_DIR/config/config.yaml:/app/backend/config/config.yaml:ro" \
  -v "$APP_DIR/config/llm_api_key:/app/backend/config/llm_api_key:ro" \
  -v "$APP_DIR/storage:/app/storage" \
  "$BACKEND_IMAGE"

docker run -d \
  --name daily-report-web \
  --restart always \
  --network daily-report-net \
  -p 80:80 \
  -v "$APP_DIR/frontend:/usr/share/nginx/html:ro" \
  -v "$APP_DIR/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine

echo "Started: http://SERVER_IP"
