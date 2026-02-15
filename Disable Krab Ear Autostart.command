#!/bin/zsh
# Выключает автозапуск Krab Ear и останавливает агент.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/remove_agent.command" "$@"
