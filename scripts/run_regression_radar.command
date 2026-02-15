#!/bin/zsh
# Запуск regression radar по локальным логам (S53).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT_DIR/scripts/regression_radar.py" "$@"
