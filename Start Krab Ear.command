#!/bin/zsh
# ------------------------------------------------------------------
# One-click запуск Krab Ear Native Agent.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/start_agent.command" "$@"
