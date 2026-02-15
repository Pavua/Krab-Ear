#!/bin/zsh
# Включает автозапуск Krab Ear через launchd.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/install_agent.command" "$@"
