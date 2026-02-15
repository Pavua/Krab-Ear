#!/bin/zsh
# Генерация self-update отчёта roadmap (S52).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT_DIR/scripts/roadmap_self_update.py" "$@"
