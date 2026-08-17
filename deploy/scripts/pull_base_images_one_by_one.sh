#!/usr/bin/env bash
set -euo pipefail

docker pull python:3.12-slim
docker pull nginx:alpine

echo "Base images are ready: python:3.12-slim, nginx:alpine"
