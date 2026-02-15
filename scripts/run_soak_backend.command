#!/bin/zsh
# ------------------------------------------------------------------
# Запуск soak-теста backend Krab Ear с автогенерацией JSON+MD отчёта.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
CYCLES="${1:-1000}"
REPORT_PATH="$REPORT_DIR/soak_backend_${TS}.json"
LATEST_JSON="$REPORT_DIR/soak_backend_latest.json"
LATEST_MD="$REPORT_DIR/soak_backend_latest.md"

if [ ! -x "$ROOT_DIR/.venv_krab_ear/bin/python" ]; then
  echo "Ошибка: .venv_krab_ear не найден. Сначала запусти Start Krab Ear.command"
  exit 1
fi

if ! [[ "$CYCLES" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: cycles должен быть целым числом, получено: $CYCLES"
  exit 1
fi

source "$ROOT_DIR/.venv_krab_ear/bin/activate"
python "$ROOT_DIR/KrabEar/tests/soak_backend.py" --cycles "$CYCLES" --report "$REPORT_PATH"

REPORT_MD="${REPORT_PATH%.json}.md"
cp "$REPORT_PATH" "$LATEST_JSON"
cp "$REPORT_MD" "$LATEST_MD"

echo
echo "✅ Soak backend завершён"
echo "Cycles:  $CYCLES"
echo "JSON:    $REPORT_PATH"
echo "MD:      $REPORT_MD"
echo "Latest:  $LATEST_JSON"
