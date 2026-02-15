#!/bin/zsh
# One-click запуск smoke-проверки релизного контура Krab Ear.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/run_smoke_release.command" "$@"
