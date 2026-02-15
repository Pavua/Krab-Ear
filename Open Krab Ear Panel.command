#!/bin/zsh
# Открывает графический интерфейс (панель управления/истории) Krab Ear.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/open_control_panel.command" "$@"
