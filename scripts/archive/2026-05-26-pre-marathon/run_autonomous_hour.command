#!/bin/zsh
# ------------------------------------------------------------------
# Длительный автономный раннер Krab Ear.
#
# Идея:
# - крутит циклы build+tests+smoke+soak заданное количество минут;
# - после каждого цикла пишет чекпоинт в отчёт;
# - останавливается только после достижения лимита времени.
#
# Пример:
#   ./scripts/run_autonomous_hour.command 60 300 2 2
# где:
#   60  = минут автономной работы
#   300 = soak-циклов на один проход
#   2   = checkpoint каждые N циклов
#   2   = аварийный stop после N подряд красных циклов
# ------------------------------------------------------------------

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
MINUTES="${1:-60}"
SOAK_CYCLES="${2:-300}"
CHECKPOINT_EVERY="${3:-2}"
MAX_FAIL_STREAK="${4:-2}"
SUMMARY="$REPORT_DIR/autonomous_hour_${TS}.md"

mkdir -p "$REPORT_DIR"

if [ ! -x "$ROOT_DIR/.venv_krab_ear/bin/python" ]; then
  echo "Ошибка: .venv_krab_ear не найден. Сначала запусти Start Krab Ear.command"
  exit 1
fi

if ! [[ "$MINUTES" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: minutes должен быть числом, получено: $MINUTES"
  exit 1
fi

if ! [[ "$SOAK_CYCLES" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: soak_cycles должен быть числом, получено: $SOAK_CYCLES"
  exit 1
fi

if ! [[ "$CHECKPOINT_EVERY" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: checkpoint_every должен быть числом, получено: $CHECKPOINT_EVERY"
  exit 1
fi

if ! [[ "$MAX_FAIL_STREAK" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: max_fail_streak должен быть числом, получено: $MAX_FAIL_STREAK"
  exit 1
fi

START_EPOCH="$(date +%s)"
END_EPOCH="$((START_EPOCH + MINUTES * 60))"
LOOP_INDEX=0
FAIL_STREAK=0
STOP_REASON="time_limit"

{
  echo "# Autonomous Hour Report — $(date -Iseconds)"
  echo
  echo "- planned_minutes: $MINUTES"
  echo "- soak_cycles_each: $SOAK_CYCLES"
  echo "- checkpoint_every_cycles: $CHECKPOINT_EVERY"
  echo "- max_fail_streak: $MAX_FAIL_STREAK"
  echo "- started_at_epoch: $START_EPOCH"
  echo
} > "$SUMMARY"

while true; do
  NOW_EPOCH="$(date +%s)"
  if [ "$NOW_EPOCH" -ge "$END_EPOCH" ]; then
    break
  fi

  LOOP_INDEX="$((LOOP_INDEX + 1))"
  CYCLE_START="$(date -Iseconds)"

  BUILD_OK="yes"
  TEST_OK="yes"
  SMOKE_OK="yes"
  SOAK_OK="yes"

  if ! swift build -c release --package-path "$ROOT_DIR/native/KrabEarAgent"; then
    BUILD_OK="no"
  fi
  if ! "$ROOT_DIR/.venv_krab_ear/bin/python" -m unittest discover -s "$ROOT_DIR/KrabEar/tests" -p "test_*.py"; then
    TEST_OK="no"
  fi
  if ! "$ROOT_DIR/scripts/run_smoke_release.command"; then
    SMOKE_OK="no"
  fi
  if ! "$ROOT_DIR/scripts/run_soak_backend.command" "$SOAK_CYCLES"; then
    SOAK_OK="no"
  fi

  if [ "$BUILD_OK" = "yes" ] && [ "$TEST_OK" = "yes" ] && [ "$SMOKE_OK" = "yes" ] && [ "$SOAK_OK" = "yes" ]; then
    FAIL_STREAK=0
  else
    FAIL_STREAK="$((FAIL_STREAK + 1))"
  fi

  CYCLE_END="$(date -Iseconds)"
  NOW_EPOCH_AFTER="$(date +%s)"
  LEFT_SEC="$((END_EPOCH - NOW_EPOCH_AFTER))"
  if [ "$LEFT_SEC" -lt 0 ]; then
    LEFT_SEC=0
  fi

  {
    echo "## Cycle $LOOP_INDEX"
    echo
    echo "- start: $CYCLE_START"
    echo "- end: $CYCLE_END"
    echo "- build_ok: $BUILD_OK"
    echo "- tests_ok: $TEST_OK"
    echo "- smoke_ok: $SMOKE_OK"
    echo "- soak_ok: $SOAK_OK"
    echo "- fail_streak: $FAIL_STREAK"
    echo "- time_left_sec: $LEFT_SEC"
    echo "- smoke_report: $(ls -1t "$REPORT_DIR"/smoke_release_*.md | head -n 1)"
    echo "- soak_report: $(ls -1t "$REPORT_DIR"/soak_backend_*.md | head -n 1)"
    echo
  } >> "$SUMMARY"

  if [ "$CHECKPOINT_EVERY" -gt 0 ] && [ $((LOOP_INDEX % CHECKPOINT_EVERY)) -eq 0 ]; then
    {
      echo "### Checkpoint $LOOP_INDEX"
      echo "- at: $(date -Iseconds)"
      echo "- fail_streak: $FAIL_STREAK"
      echo "- time_left_sec: $LEFT_SEC"
      echo
    } >> "$SUMMARY"
  fi

  if [ "$MAX_FAIL_STREAK" -gt 0 ] && [ "$FAIL_STREAK" -ge "$MAX_FAIL_STREAK" ]; then
    STOP_REASON="fail_streak_reached"
    break
  fi
done

FINISH_EPOCH="$(date +%s)"
ACTUAL_SEC="$((FINISH_EPOCH - START_EPOCH))"

{
  echo "## Summary"
  echo
  echo "- cycles_completed: $LOOP_INDEX"
  echo "- actual_seconds: $ACTUAL_SEC"
  echo "- stop_reason: $STOP_REASON"
  echo "- finished_at: $(date -Iseconds)"
} >> "$SUMMARY"

echo "✅ Автономный часовой раннер завершён"
echo "Отчёт: $SUMMARY"
