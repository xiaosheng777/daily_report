#!/usr/bin/env bash
# Compatibility entry point: keep every operational deployment command under
# deploy/scripts while the bundle's root install.sh remains the implementation.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec bash "$PROJECT_ROOT/install.sh" "$@"
