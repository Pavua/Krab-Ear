#!/bin/zsh
# ------------------------------------------------------------------
# Smoke-check релизного контура Krab Ear Agent:
# 1) старт агента;
# 2) запрос штатного завершения СВОЕГО агента (по PID, не рассылкой);
# 3) корректное завершение и отсутствие висящих процессов.
#
# Владение процессами (G2 Ear 02a F01 / W6-B, ревью ChatGPT):
# - перед стартом скрипт требует, чтобы на машине не было ни одного агента Ear
#   (по имени исполняемого файла, в любом каталоге/бандле), ни backend этого
#   каталога, ни его штатного сокета; иначе отказ без единого kill и без запросов;
# - очистка и запрос завершения касаются только PID, появившихся после этой
#   проверки, и только пока их командная строка совпадает с шаблоном. Никакого
#   pkill по шаблону и никакой рассылки control-уведомлений (quit через
#   NSDistributedNotificationCenter получили бы ВСЕ агенты на машине).
# - Известный остаток: процесс, который чужой launchd поднял в окне между проверкой и
#   нашим стартом, неотличим от нашего (агент запускается через LaunchServices и не
#   наш потомок). Устранить можно только токеном запуска, который принимает агент.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT_PATH="$REPORT_DIR/smoke_release_${TS}.md"
START_LOG="$REPORT_DIR/smoke_release_${TS}.start.log"

# Агент: по имени исполняемого файла, где бы он ни лежал (бандл Krab Ear.app, native/runtime,
# другой checkout). Живой агент запускается как .../Krab Ear.app/Contents/MacOS/KrabEarAgent
# БЕЗ --project-root, поэтому шаблон с путём этого каталога его не видел.
AGENT_PATTERN="${KRAB_SMOKE_AGENT_PATTERN_OVERRIDE:-(^|/)KrabEarAgent( |$)}"
# S3/Р9: standalone active-режим (BackendSupervisor.swift) теперь спавнит
# main.py, а не backend/service.py напрямую — но старый launchd-плист/старая
# установка могли ещё гонять backend/service.py. ERE-альтернация (pgrep на
# macOS = ERE, не BRE — см. test_ensure_agent_running_contract.py) матчит оба,
# иначе смок сочтёт живой backend отсутствующим.
BACKEND_PATTERN="$ROOT_DIR/KrabEar/(backend/service|main)\.py"
SOCKET_PATH="${KRAB_EAR_SOCKET:-$HOME/Library/Application Support/KrabEar/krabear.sock}"

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

if foreign_present "$AGENT_PATTERN" || foreign_present "$BACKEND_PATTERN" || [ -e "$SOCKET_PATH" ]; then
  echo "Ошибка: на машине уже есть агент Krab Ear, backend этого каталога или его сокет (или pgrep недоступен)."
  echo "Smoke-check не стартует и ничего не останавливает: остановите экземпляр сами."
  exit 1
fi

mkdir -p "$REPORT_DIR"

# Всегда код 0: при set -e «нет совпадений» (pgrep=1) не должно обрывать скрипт.
pids_of() {
  pgrep -f "$1" 2>/dev/null || true
}

# Запрос штатного завершения (как Cmd+Q) КАЖДОМУ нашему агенту по PID. Без рассылки.
terminate_own_agents() {
  local pid sent=1
  for pid in $(pids_of "$AGENT_PATTERN"); do
    if "$ROOT_DIR/.venv_krab_ear/bin/python" - "$pid" <<'PY'
import sys
from AppKit import NSRunningApplication

app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(sys.argv[1]))
sys.exit(0 if app is not None and app.terminate() else 1)
PY
    then
      sent=0
    fi
  done
  return "$sent"
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
# preflight (до него совпадений не было, значит это наши процессы; см. известный остаток выше).
OWN_PIDS=""
START_PID=""
CLEANED_UP="no"
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
  # Однократно: EXIT срабатывает и после обработчика сигнала.
  [ "$CLEANED_UP" = "yes" ] && return 0
  CLEANED_UP="yes"
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
# В zsh необработанные INT/TERM/HUP завершают оболочку без EXIT-ловушки: превращаем их в exit.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

set +e
"$ROOT_DIR/scripts/start_agent.command" --launched-by-launchd >"$START_LOG" 2>&1 &
START_PID=$!
set -e

START_OK="no"
BACKEND_OK="no"
CONTROL_OK="no"
SHUTDOWN_OK="no"
# Preflight гарантировал нулевой baseline: честное сравнение «после завершения должно быть 0».
BASE_AGENT_COUNT=0
BASE_BACKEND_COUNT=0

if wait_for_pattern "$AGENT_PATTERN" "$WAIT_START"; then
  START_OK="yes"
fi

if wait_for_pattern "$BACKEND_PATTERN" "$WAIT_BACKEND"; then
  BACKEND_OK="yes"
fi

if [ "$START_OK" = "yes" ] && [ "$BACKEND_OK" = "yes" ]; then
  if terminate_own_agents; then
    CONTROL_OK="yes"
  fi
fi

if wait_until_stopped "$AGENT_PATTERN" "$WAIT_STOP" && wait_until_stopped "$BACKEND_PATTERN" "$WAIT_STOP"; then
  SHUTDOWN_OK="yes"
else
  # Мягкий повтор запроса завершения и ещё немного ожидания; без fallback «не хуже baseline».
  terminate_own_agents || true
  if wait_until_stopped "$AGENT_PATTERN" "$WAIT_STOP_RETRY" && wait_until_stopped "$BACKEND_PATTERN" "$WAIT_STOP_RETRY"; then
    SHUTDOWN_OK="yes"
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
