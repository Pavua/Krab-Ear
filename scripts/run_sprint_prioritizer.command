#!/bin/zsh
# Запуск скоринга roadmap-спринтов (S51).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT_DIR/scripts/score_roadmap_sprints.py" "$@"
