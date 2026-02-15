#!/bin/zsh
# One-click остановка внешнего Krab Voice Gateway.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/stop_voice_gateway.command" "$@"
