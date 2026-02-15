#!/bin/zsh
# One-click запуск boundary-check для Codex/Antigravity.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_agent_boundary_check.command" "$@"
