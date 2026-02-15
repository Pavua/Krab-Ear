#!/bin/zsh
# ------------------------------------------------------------------
# Release checklist Krab Ear (fail-fast):
# 1) shell lint .command-скриптов
# 2) swift release build
# 3) backend unit tests
# 4) release smoke
# 5) отчёт в docs/reports/release_checklist_*.md
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT_PATH="$REPORT_DIR/release_checklist_${TS}.md"
VENV_PY="$ROOT_DIR/.venv_krab_ear/bin/python"

mkdir -p "$REPORT_DIR"

fail() {
  local step="$1"
  local message="$2"
  {
    echo "# Release Checklist Report — $(date -Iseconds)"
    echo
    echo "- status: **FAILED**"
    echo "- failed_step: $step"
    echo "- message: $message"
  } > "$REPORT_PATH"
  echo "❌ Release checklist FAILED ($step)"
  echo "Отчёт: $REPORT_PATH"
  exit 1
}

[ -x "$VENV_PY" ] || fail "preflight" "Не найден venv python: $VENV_PY"

SHELL_OK="no"
BUILD_OK="no"
TESTS_OK="no"
SMOKE_OK="no"
SMOKE_REPORT="-"

if find "$ROOT_DIR" -maxdepth 2 -name "*.command" -print0 | xargs -0 -n1 zsh -n; then
  SHELL_OK="yes"
else
  fail "shell_lint" "Один или несколько .command скриптов содержат синтаксическую ошибку"
fi

if swift build -c release --package-path "$ROOT_DIR/native/KrabEarAgent"; then
  BUILD_OK="yes"
else
  fail "swift_build" "Swift release build завершился с ошибкой"
fi

if "$VENV_PY" -m unittest discover -s "$ROOT_DIR/KrabEar/tests" -v; then
  TESTS_OK="yes"
else
  fail "unit_tests" "Backend unit tests завершились с ошибкой"
fi

if "$ROOT_DIR/scripts/run_smoke_release.command"; then
  SMOKE_OK="yes"
  SMOKE_REPORT="$(ls -1t "$REPORT_DIR"/smoke_release_*.md 2>/dev/null | head -n 1 || true)"
  [ -n "$SMOKE_REPORT" ] || SMOKE_REPORT="-"
else
  fail "release_smoke" "Smoke-check завершился с ошибкой"
fi

{
  echo "# Release Checklist Report — $(date -Iseconds)"
  echo
  echo "- status: **OK**"
  echo "- shell_lint: $SHELL_OK"
  echo "- swift_release_build: $BUILD_OK"
  echo "- backend_unit_tests: $TESTS_OK"
  echo "- release_smoke: $SMOKE_OK"
  echo "- smoke_report: $SMOKE_REPORT"
} > "$REPORT_PATH"

echo "✅ Release checklist OK"
echo "Отчёт: $REPORT_PATH"
