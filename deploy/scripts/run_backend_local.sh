#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../backend"
python -m src.web.app --host 0.0.0.0 --port 8000
