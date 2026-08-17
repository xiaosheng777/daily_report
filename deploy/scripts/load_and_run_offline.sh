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
docker rm -f daily-report-web daily-report-monitor daily-report-worker daily-report-backend >/dev/null 2>&1 || true
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
  --name daily-report-worker \
  --restart always \
  --network daily-report-net \
  --cpus 2 \
  --memory 8g \
  -v "$APP_DIR/config/config.yaml:/app/backend/config/config.yaml:ro" \
  -v "$APP_DIR/config/llm_api_key:/app/backend/config/llm_api_key:ro" \
  -v "$APP_DIR/storage:/app/storage" \
  "$BACKEND_IMAGE" python -m src.worker --config config/config.yaml

docker run -d \
  --name daily-report-web \
  --restart always \
  --network daily-report-net \
  -p 80:80 \
  -v "$APP_DIR/frontend:/usr/share/nginx/html:ro" \
  -v "$APP_DIR/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine

docker run -d \
  --name daily-report-monitor \
  --restart always \
  --network daily-report-net \
  --cpus 0.5 \
  --memory 256m \
  -v "$APP_DIR/config/config.yaml:/app/backend/config/config.yaml:ro" \
  -v "$APP_DIR/storage:/app/storage" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /proc:/host/proc:ro \
  -v /sys/fs/cgroup:/host/cgroup:ro \
  "$BACKEND_IMAGE" python -m src.monitoring.collector --config config/config.yaml

echo "Started: http://SERVER_IP"
