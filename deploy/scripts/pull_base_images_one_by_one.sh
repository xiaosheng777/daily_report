#!/usr/bin/env bash
set -euo pipefail

docker pull python:3.12-slim
docker pull eclipse-temurin:21-jdk
docker pull nginx:alpine

echo "Base images are ready: python:3.12-slim, eclipse-temurin:21-jdk, nginx:alpine"
