#!/bin/zsh
# One-click запуск soak-теста backend.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_soak_backend.command" "$@"
