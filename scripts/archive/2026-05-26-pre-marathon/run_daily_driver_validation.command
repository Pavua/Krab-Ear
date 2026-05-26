#!/bin/zsh
# ------------------------------------------------------------------
# Daily Driver Validation (S24):
# 1) release checklist;
# 2) короткий backend-soak;
# 3) сводный отчёт с рисками в docs/reports.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
SOAK_CYCLES="${1:-300}"
REPORT_PATH="$REPORT_DIR/daily_driver_validation_${TS}.md"

mkdir -p "$REPORT_DIR"

if ! [[ "$SOAK_CYCLES" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: soak_cycles должен быть числом, получено: $SOAK_CYCLES"
  exit 1
fi

STATUS="OK"
CHECKLIST_OK="yes"
SOAK_OK="yes"

if ! "$ROOT_DIR/scripts/run_release_checklist.command" >/dev/null; then
  CHECKLIST_OK="no"
  STATUS="FAILED"
fi

if ! "$ROOT_DIR/scripts/run_soak_backend.command" "$SOAK_CYCLES" >/dev/null; then
  SOAK_OK="no"
  STATUS="FAILED"
fi

LATEST_CHECKLIST="$(ls -1t "$REPORT_DIR"/release_checklist_*.md 2>/dev/null | head -n 1 || true)"
LATEST_SMOKE="$(ls -1t "$REPORT_DIR"/smoke_release_*.md 2>/dev/null | head -n 1 || true)"
LATEST_SOAK="$(ls -1t "$REPORT_DIR"/soak_backend_*.md 2>/dev/null | head -n 1 || true)"

{
  echo "# Daily Driver Validation — $(date -Iseconds)"
  echo
  echo "- status: **$STATUS**"
  echo "- checklist_ok: $CHECKLIST_OK"
  echo "- backend_soak_ok: $SOAK_OK"
  echo "- soak_cycles: $SOAK_CYCLES"
  echo "- checklist_report: ${LATEST_CHECKLIST:-"-"}"
  echo "- smoke_report: ${LATEST_SMOKE:-"-"}"
  echo "- soak_report: ${LATEST_SOAK:-"-"}"
  echo
  echo "## Residual Risks"
  echo
  if [ "$CHECKLIST_OK" != "yes" ]; then
    echo "1. Release checklist не прошёл полностью."
  fi
  if [ "$SOAK_OK" != "yes" ]; then
    echo "2. Backend soak выявил нестабильность."
  fi
  if [ "$CHECKLIST_OK" = "yes" ] && [ "$SOAK_OK" = "yes" ]; then
    echo "1. Критичных рисков не выявлено в этом прогоне."
    echo "2. Остаётся долгосрочный риск регрессий без 10k soak-пакета."
  fi
} > "$REPORT_PATH"

if [ "$STATUS" != "OK" ]; then
  echo "❌ Daily Driver Validation FAILED"
  echo "Отчёт: $REPORT_PATH"
  exit 1
fi

echo "✅ Daily Driver Validation OK"
echo "Отчёт: $REPORT_PATH"
