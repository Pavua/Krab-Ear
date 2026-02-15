#!/bin/zsh
# Генерация локального UX telemetry отчёта.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT_DIR/scripts/collect_ux_telemetry.py" "$@"
