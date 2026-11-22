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
# «latest» — согласованная пара: оба отчёта должны существовать, публикуем через
# временные файлы в том же каталоге + rename (отказ второго cp больше не оставляет
# новый json рядом со старым md, и читатель не видит наполовину записанный файл).
[ -f "$REPORT_PATH" ] || { echo "Ошибка: soak не создал JSON-отчёт: $REPORT_PATH"; exit 1; }
[ -f "$REPORT_MD" ] || { echo "Ошибка: soak не создал MD-отчёт: $REPORT_MD"; exit 1; }
TMP_JSON="$(mktemp "$REPORT_DIR/.latest_json.XXXXXX")"
TMP_MD="$(mktemp "$REPORT_DIR/.latest_md.XXXXXX")"
trap 'rm -f "$TMP_JSON" "$TMP_MD"' EXIT
cp "$REPORT_PATH" "$TMP_JSON"
cp "$REPORT_MD" "$TMP_MD"
chmod 644 "$TMP_JSON" "$TMP_MD"
mv -f "$TMP_JSON" "$LATEST_JSON"
mv -f "$TMP_MD" "$LATEST_MD"

echo
echo "✅ Soak backend завершён"
echo "Cycles:  $CYCLES"
echo "JSON:    $REPORT_PATH"
echo "MD:      $REPORT_MD"
echo "Latest:  $LATEST_JSON"
