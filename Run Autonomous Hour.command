#!/bin/zsh
# ------------------------------------------------------------------
# Ярлык для длительного автономного прогона Krab Ear.
# По умолчанию: 60 минут, soak=300, checkpoint=2, max_fail_streak=2.
# Пример:
#   ./Run\ Autonomous\ Hour.command 90 500 3 2
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$ROOT_DIR/scripts/run_autonomous_hour.command" "$@"
