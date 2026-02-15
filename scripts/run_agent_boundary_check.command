#!/bin/zsh
# Проверка, что агент не вышел за пределы своей зоны ответственности.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="${1:-${KRAB_AGENT_OWNER:-codex}}"

if [[ "$OWNER" != "codex" && "$OWNER" != "antigravity" ]]; then
  echo "Ошибка: owner должен быть codex или antigravity"
  exit 1
fi

shift || true
python3 "$ROOT_DIR/scripts/check_agent_boundaries.py" "$OWNER" "$@"
