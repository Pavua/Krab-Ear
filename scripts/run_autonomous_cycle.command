#!/bin/zsh
# ------------------------------------------------------------------
# Автономный цикл проверки Krab Ear:
# 1) swift release build
# 2) python unit tests
# 3) release smoke
# 4) backend soak
# Сохраняет сводный отчёт в docs/reports.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
SOAK_CYCLES="${1:-500}"
LOOPS="${2:-1}"
SUMMARY="$REPORT_DIR/autonomous_cycle_${TS}.md"

mkdir -p "$REPORT_DIR"

if [ ! -x "$ROOT_DIR/.venv_krab_ear/bin/python" ]; then
  echo "Ошибка: .venv_krab_ear не найден. Сначала запусти Start Krab Ear.command"
  exit 1
fi

if ! [[ "$SOAK_CYCLES" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: soak_cycles должен быть числом, получено: $SOAK_CYCLES"
  exit 1
fi

if ! [[ "$LOOPS" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: loops должен быть числом, получено: $LOOPS"
  exit 1
fi

{
  echo "# Autonomous Cycle Report — $(date -Iseconds)"
  echo
  echo "- loops: $LOOPS"
  echo "- soak_cycles_each: $SOAK_CYCLES"
  echo
} > "$SUMMARY"

for ((i=1; i<=LOOPS; i++)); do
  echo "==> Цикл $i/$LOOPS"
  CYCLE_START="$(date -Iseconds)"

  swift build -c release --package-path "$ROOT_DIR/native/KrabEarAgent"
  "$ROOT_DIR/.venv_krab_ear/bin/python" -m unittest discover -s "$ROOT_DIR/KrabEar/tests" -v
  "$ROOT_DIR/scripts/run_smoke_release.command"
  "$ROOT_DIR/scripts/run_soak_backend.command" "$SOAK_CYCLES"

  CYCLE_END="$(date -Iseconds)"
  {
    echo "## Цикл $i"
    echo
    echo "- start: $CYCLE_START"
    echo "- end: $CYCLE_END"
    echo "- smoke_report: $(ls -1t "$REPORT_DIR"/smoke_release_*.md | head -n 1)"
    echo "- soak_report: $(ls -1t "$REPORT_DIR"/soak_backend_*.md | head -n 1)"
    echo
  } >> "$SUMMARY"
done

echo "✅ Автономный цикл завершён"
echo "Сводный отчёт: $SUMMARY"
