#!/bin/zsh
# Генерация health-отчёта истории Krab Ear.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT_DIR/scripts/history_health_report.py" "$@"
