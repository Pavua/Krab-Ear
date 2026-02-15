#!/bin/zsh
# One-click открытие папки отчётов Krab Ear.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/open_reports.command" "$@"
