#!/bin/zsh
# ------------------------------------------------------------------
# Открывает папку отчётов Krab Ear (smoke/soak) в Finder.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
mkdir -p "$REPORT_DIR"
open "$REPORT_DIR"
