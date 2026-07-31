#!/bin/zsh
# ------------------------------------------------------------------
# Smoke-check релизного контура Krab Ear Agent:
# 1) старт агента;
# 2) базовые control-команды;
# 3) корректное завершение и отсутствие висящих процессов.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT_PATH="$REPORT_DIR/smoke_release_${TS}.md"
START_LOG="$REPORT_DIR/smoke_release_${TS}.start.log"

AGENT_BIN_PATTERN="$ROOT_DIR/native/runtime/KrabEarAgent"
AGENT_PATTERN="$ROOT_DIR/native/runtime/KrabEarAgent --project-root $ROOT_DIR"
# S3/Р9: standalone active-режим (BackendSupervisor.swift) теперь спавнит
# main.py, а не backend/service.py напрямую — но старый launchd-плист/старая
# установка могли ещё гонять backend/service.py. ERE-альтернация (pgrep на
# macOS = ERE, не BRE — см. test_ensure_agent_running_contract.py) матчит оба,
# иначе смок сочтёт живой backend отсутствующим.
BACKEND_PATTERN="$ROOT_DIR/KrabEar/(backend/service|main)\.py"

mkdir -p "$REPORT_DIR"

send_control_action() {
  local action="$1"
  "$ROOT_DIR/.venv_krab_ear/bin/python" - "$action" <<'PY'
import sys
from Foundation import NSDistributedNotificationCenter

action = sys.argv[1]
NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
    "com.krabear.agent.control",
    None,
    {"action": action},
    True,
)
PY
}

wait_for_pattern() {
  local pattern="$1"
  local timeout="$2"
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if pgrep -f "$pattern" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

count_pattern() {
  local pattern="$1"
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    pgrep -f "$pattern" | wc -l | tr -d ' '
  else
    echo "0"
  fi
}

wait_until_stopped() {
  local pattern="$1"
  local timeout="$2"
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if ! pgrep -f "$pattern" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

cleanup() {
  pkill -f "$AGENT_BIN_PATTERN" >/dev/null 2>&1 || true
  pkill -f "$BACKEND_PATTERN" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ ! -x "$ROOT_DIR/.venv_krab_ear/bin/python" ]; then
  echo "Ошибка: .venv_krab_ear не найден. Сначала запусти Start Krab Ear.command"
  exit 1
fi

set +e
"$ROOT_DIR/scripts/start_agent.command" --launched-by-launchd >"$START_LOG" 2>&1 &
START_PID=$!
set -e

START_OK="no"
BACKEND_OK="no"
CONTROL_OK="no"
SHUTDOWN_OK="no"
BASE_AGENT_COUNT="$(count_pattern "$AGENT_PATTERN")"
BASE_BACKEND_COUNT="$(count_pattern "$BACKEND_PATTERN")"

if wait_for_pattern "$AGENT_PATTERN" 15; then
  START_OK="yes"
fi

if wait_for_pattern "$BACKEND_PATTERN" 15; then
  BACKEND_OK="yes"
fi

if [ "$START_OK" = "yes" ] && [ "$BACKEND_OK" = "yes" ]; then
  send_control_action "show_history" || true
  sleep 1
  send_control_action "quit" || true
  sleep 1
  send_control_action "quit" || true
  CONTROL_OK="yes"
fi

if wait_until_stopped "$AGENT_PATTERN" 12 && wait_until_stopped "$BACKEND_PATTERN" 12; then
  SHUTDOWN_OK="yes"
else
  # Мягкий fallback: повторно посылаем quit и ждём ещё немного.
  send_control_action "quit" || true
  if wait_until_stopped "$AGENT_PATTERN" 6 && wait_until_stopped "$BACKEND_PATTERN" 6; then
    SHUTDOWN_OK="yes"
  else
    # Если автозапуск launchd активен, допускаем возврат к исходному baseline.
    CUR_AGENT_COUNT="$(count_pattern "$AGENT_PATTERN")"
    CUR_BACKEND_COUNT="$(count_pattern "$BACKEND_PATTERN")"
    if [ "$CUR_AGENT_COUNT" -le "$BASE_AGENT_COUNT" ] && [ "$CUR_BACKEND_COUNT" -le "$BASE_BACKEND_COUNT" ]; then
      SHUTDOWN_OK="yes"
    fi
  fi
fi

STATUS="FAILED"
if [ "$START_OK" = "yes" ] && [ "$BACKEND_OK" = "yes" ] && [ "$CONTROL_OK" = "yes" ] && [ "$SHUTDOWN_OK" = "yes" ]; then
  STATUS="OK"
fi

{
  echo "# Smoke Release Report — $(date -Iseconds)"
  echo
  echo "- status: **$STATUS**"
  echo "- start_ok: $START_OK"
  echo "- backend_ok: $BACKEND_OK"
  echo "- control_ok: $CONTROL_OK"
  echo "- shutdown_ok: $SHUTDOWN_OK"
  echo "- base_agent_count: $BASE_AGENT_COUNT"
  echo "- base_backend_count: $BASE_BACKEND_COUNT"
  echo "- current_agent_count: $(count_pattern "$AGENT_PATTERN")"
  echo "- current_backend_count: $(count_pattern "$BACKEND_PATTERN")"
  echo "- start_pid: $START_PID"
  echo "- start_log: $(basename "$START_LOG")"
} > "$REPORT_PATH"

if [ "$STATUS" != "OK" ]; then
  echo "❌ Smoke-check FAILED"
  echo "Отчёт: $REPORT_PATH"
  exit 1
fi

echo "✅ Smoke-check OK"
echo "Отчёт: $REPORT_PATH"
