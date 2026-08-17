#!/usr/bin/env bash
# Build a self-contained, Docker-based offline deployment bundle.
# Run this on a Linux/x86_64 machine that has Docker and the required base
# images locally available. The target server only needs Docker + Compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUTPUT_DIR="${1:-$PROJECT_ROOT/dist}"
VERSION="${VERSION:-$(date +%Y%m%d%H%M%S)}"
BUNDLE_NAME="daily-report-offline-${VERSION}"
BUNDLE_DIR="$OUTPUT_DIR/$BUNDLE_NAME"
ARCHIVE_PATH="$OUTPUT_DIR/${BUNDLE_NAME}.tar.gz"
BACKEND_IMAGE="${BACKEND_IMAGE:-daily-report-backend:latest}"
NGINX_IMAGE="${NGINX_IMAGE:-nginx:alpine}"
TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

require_command docker
require_command tar

for image in python:3.12-slim "$NGINX_IMAGE"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "Required local Docker image is missing: $image" >&2
    echo "Pull it on this connected build machine, then run this script again." >&2
    exit 1
  fi
done

mkdir -p "$OUTPUT_DIR"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/images" "$BUNDLE_DIR/config"

echo "Building $BACKEND_IMAGE for $TARGET_PLATFORM without pulling from the network..."
docker build --platform="$TARGET_PLATFORM" --pull=false -f "$PROJECT_ROOT/deploy/Dockerfile.backend" -t "$BACKEND_IMAGE" "$PROJECT_ROOT"

echo "Exporting runtime images..."
# The backend image already contains Python packages and the JDK copied from
# the builder stage. The target needs only these two runtime image tags.
docker save -o "$BUNDLE_DIR/images/daily-report-images.tar" "$BACKEND_IMAGE" "$NGINX_IMAGE"
printf '%s\n' "$BACKEND_IMAGE" "$NGINX_IMAGE" > "$BUNDLE_DIR/images/images.txt"

echo "Collecting runtime files..."
tar -C "$PROJECT_ROOT" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
  --exclude='backend/config/llm_api_key' --exclude='config/llm_api_key' \
  -cf - backend frontend wheelhouse deploy docker-compose.yml DEPLOY.md OFFLINE_UPGRADE_GUIDE.md README.md install.sh \
  | tar -C "$BUNDLE_DIR" -xf -

# Do not distribute the build-machine API key. The installer turns this into
# config.yaml only when the target does not already have one.
cp "$PROJECT_ROOT/config/config.yaml" "$BUNDLE_DIR/config/config.yaml.example"
chmod +x "$BUNDLE_DIR/install.sh" "$BUNDLE_DIR"/deploy/scripts/*.sh

cat > "$BUNDLE_DIR/MANIFEST.txt" <<EOF
Daily Report offline deployment bundle
Version: $VERSION
Runtime images: $BACKEND_IMAGE, $NGINX_IMAGE
Platform: $TARGET_PLATFORM
Generated at: $(date -Iseconds)

Target prerequisites: Linux x86_64, Docker Engine, and Docker Compose v2
(or the docker-compose v1 command). No registry/network access is needed.
EOF

tar -C "$OUTPUT_DIR" -czf "$ARCHIVE_PATH" "$BUNDLE_NAME"
echo ""
echo "Bundle created: $ARCHIVE_PATH"
echo "Transfer this tar.gz to the offline server, extract it, then run:"
echo "  sudo bash ./$BUNDLE_NAME/deploy/scripts/install.sh /opt/daily-report"
