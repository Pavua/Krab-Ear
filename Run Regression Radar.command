#!/bin/zsh
# One-click запуск regression radar.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_regression_radar.command" "$@"
