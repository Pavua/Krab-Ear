#!/bin/zsh
# Проверка performance budget на базе последней UX telemetry.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT_DIR/scripts/check_performance_budget.py" "$@"
