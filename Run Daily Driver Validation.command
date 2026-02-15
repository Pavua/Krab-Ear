#!/bin/zsh
# One-click запуск Daily Driver Validation (S24).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_daily_driver_validation.command" "$@"
