#!/bin/zsh
# ------------------------------------------------------------------
# One-click сценарий:
# 1) форс-обновление нативного агента;
# 2) открытие панели управления/истории.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$ROOT_DIR/Update Krab Ear Agent.command"
"$ROOT_DIR/Open Krab Ear Panel.command"
