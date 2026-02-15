#!/bin/zsh
# One-click запуск внешнего Krab Voice Gateway.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/start_voice_gateway.command" "$@"
