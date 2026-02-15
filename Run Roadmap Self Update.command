#!/bin/zsh
# One-click запуск self-update отчёта roadmap.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_roadmap_self_update.command" "$@"
