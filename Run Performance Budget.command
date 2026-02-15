#!/bin/zsh
# One-click запуск проверки performance budget.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_performance_budget.command" "$@"
