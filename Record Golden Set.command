#!/bin/zsh
# Record Golden Set — запись эталонного набора R2 (50 фраз RU/ES/EN).
# Двойной клик → интерактивная запись. Фразы: docs/golden/r2-scenario.md.
# Аудио остаётся локально (~/Library/Application Support/KrabEar/golden/).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$ROOT_DIR/scripts/record_golden.py" "$@"
