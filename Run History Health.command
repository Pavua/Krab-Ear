#!/bin/zsh
# One-click запуск health-отчёта истории.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_history_health.command" "$@"
