#!/bin/zsh
# One-click запуск скоринга roadmap-спринтов.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_sprint_prioritizer.command" "$@"
