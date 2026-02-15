#!/bin/zsh
# One-click запуск UX telemetry.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_ux_telemetry.command" "$@"
