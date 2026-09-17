#!/bin/zsh
# Record Golden Set — запись эталонного набора R2 (50 фраз RU/ES/EN).
# Двойной клик → интерактивная запись. Фразы: docs/golden/r2-scenario.md.
# Аудио остаётся локально (~/Library/Application Support/KrabEar/golden/).

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$ROOT_DIR/scripts/record_golden.py" "$@"
rc=$?
echo
echo "Нажми Enter, чтобы закрыть окно."
read -r
exit $rc
