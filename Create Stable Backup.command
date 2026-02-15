#!/bin/zsh
# Создаёт резервный снимок текущей стабильной версии Krab Ear.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/create_stable_backup.command" "$@"
