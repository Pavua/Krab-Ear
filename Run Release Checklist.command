#!/bin/zsh
# One-click запуск полного release checklist для Krab Ear.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_release_checklist.command" "$@"
