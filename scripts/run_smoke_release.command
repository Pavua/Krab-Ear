#!/bin/zsh
# ------------------------------------------------------------------
# Smoke-check релизного контура Krab Ear Agent:
# 1) старт агента;
# 2) базовые control-команды;
# 3) корректное завершение и отсутствие висящих процессов.
#
# Владение процессами (G2 Ear 02a F01 / W6-B): скрипт НЕ останавливает ничего по
# шаблону. Перед стартом он требует, чтобы в этом каталоге не было ни агента, ни
# backend (иначе отказ без единого kill), а очистка завершает только те PID, которые
# появились после этой проверки (и сам фоновый старт). Живые процессы владельца под
# шаблон «убить всё совпавшее» больше не попадают.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT_PATH="$REPORT_DIR/smoke_release_${TS}.md"
START_LOG="$REPORT_DIR/smoke_release_${TS}.start.log"

AGENT_PATTERN="$ROOT_DIR/native/runtime/KrabEarAgent --project-root $ROOT_DIR"
# S3/Р9: standalone active-режим (BackendSupervisor.swift) теперь спавнит
# main.py, а не backend/service.py напрямую — но старый launchd-плист/старая
# установка могли ещё гонять backend/service.py. ERE-альтернация (pgrep на
# macOS = ERE, не BRE — см. test_ensure_agent_running_contract.py) матчит оба,
# иначе смок сочтёт живой backend отсутствующим.
BACKEND_PATTERN="$ROOT_DIR/KrabEar/(backend/service|main)\.py"

# Таймауты ожидания; переопределяются в тестах на заглушках.
WAIT_START="${KRAB_SMOKE_WAIT_START:-15}"
WAIT_BACKEND="${KRAB_SMOKE_WAIT_BACKEND:-15}"
WAIT_STOP="${KRAB_SMOKE_WAIT_STOP:-12}"
WAIT_STOP_RETRY="${KRAB_SMOKE_WAIT_STOP_RETRY:-6}"

# Preflight ДО любых побочных действий и ДО trap очистки: ошибка здесь ничего не трогает.
if [ ! -x "$ROOT_DIR/.venv_krab_ear/bin/python" ]; then
  echo "Ошибка: .venv_krab_ear не найден. Сначала запусти Start Krab Ear.command"
  exit 1
fi

# Код 0 — совпадения есть, 1 — нет, иное — pgrep не отработал (считаем как «есть»: fail-closed).
foreign_present() {
  local pattern="$1"
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    return 0
  else
    [ "$?" -eq 1 ] && return 1
    return 0
  fi
}

if foreign_present "$AGENT_PATTERN" || foreign_present "$BACKEND_PATTERN"; then
  echo "Ошибка: в $ROOT_DIR уже запущен агент или backend Krab Ear (или pgrep недоступен)."
  echo "Smoke-check не стартует и ничего не останавливает: остановите экземпляр сами."
  exit 1
fi

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
      remember_own_pids
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

# PID, принадлежащие этому запуску: фоновый старт и всё, что совпало с шаблонами ПОСЛЕ
# preflight (до него совпадений не было, значит это наши процессы).
OWN_PIDS=""
START_PID=""
# Всегда код 0: при set -e «нет совпадений» (pgrep=1) не должно обрывать очистку.
pids_of() {
  pgrep -f "$1" 2>/dev/null || true
}
remember_own_pids() {
  local pid
  for pid in $(pids_of "$AGENT_PATTERN") $(pids_of "$BACKEND_PATTERN"); do
    case " $OWN_PIDS " in
      *" $pid "*) ;;
      *) OWN_PIDS="$OWN_PIDS $pid" ;;
    esac
  done
}

cleanup() {
  # Только штатный TERM и только по запомненным PID: никакого pkill по шаблону.
  # PID мог умереть и быть выдан другому процессу, поэтому убиваем лишь тех, кто
  # всё ещё совпадает с шаблоном (или фоновый старт, пока его командная строка наша).
  local pid current start_cmd
  remember_own_pids
  current=" $(pids_of "$AGENT_PATTERN") $(pids_of "$BACKEND_PATTERN") "
  current="${current//$'\n'/ }"
  # zsh не делит неэкранированную переменную на слова: разбиение явное, через ${=...}.
  for pid in ${=OWN_PIDS}; do
    case "$current" in
      *" $pid "*) kill "$pid" >/dev/null 2>&1 || true ;;
    esac
  done
  if [ -n "$START_PID" ]; then
    # Без конвейера: при pipefail ранний выход grep -q мог дать ложный отказ.
    start_cmd="$(ps -p "$START_PID" -o command= 2>/dev/null || true)"
    case "$start_cmd" in
      *"$ROOT_DIR/scripts/start_agent.command"*) kill "$START_PID" >/dev/null 2>&1 || true ;;
    esac
  fi
}
trap cleanup EXIT

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

if wait_for_pattern "$AGENT_PATTERN" "$WAIT_START"; then
  START_OK="yes"
fi

if wait_for_pattern "$BACKEND_PATTERN" "$WAIT_BACKEND"; then
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

if wait_until_stopped "$AGENT_PATTERN" "$WAIT_STOP" && wait_until_stopped "$BACKEND_PATTERN" "$WAIT_STOP"; then
  SHUTDOWN_OK="yes"
else
  # Мягкий fallback: повторно посылаем quit и ждём ещё немного.
  send_control_action "quit" || true
  if wait_until_stopped "$AGENT_PATTERN" "$WAIT_STOP_RETRY" && wait_until_stopped "$BACKEND_PATTERN" "$WAIT_STOP_RETRY"; then
    SHUTDOWN_OK="yes"
  else
    # Preflight гарантировал нулевой baseline, но оставляем сравнение как защиту.
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
