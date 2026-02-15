#!/bin/zsh
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$ROOT_DIR/scripts/update_agent.command"
