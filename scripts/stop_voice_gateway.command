#!/bin/zsh
# ------------------------------------------------------------------
# Остановка внешнего Krab Voice Gateway из репозитория Krab Ear.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GATEWAY_DIR="${KRAB_VOICE_GATEWAY_DIR:-$ROOT_DIR/../Krab Voice Gateway}"

if [ ! -d "$GATEWAY_DIR" ]; then
  echo "Ошибка: директория Krab Voice Gateway не найдена: $GATEWAY_DIR"
  exit 1
fi

exec "$GATEWAY_DIR/scripts/stop_gateway.command" "$@"
