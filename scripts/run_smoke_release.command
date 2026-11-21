#!/bin/zsh
# ------------------------------------------------------------------
# Smoke-check релизного контура Krab Ear Agent:
# 1) старт агента;
# 2) запрос штатного завершения СВОЕГО агента (по PID, не рассылкой);
# 3) корректное завершение и отсутствие висящих процессов.
#
# Владение процессами (G2 Ear 02a F01 / W6-B, ревью ChatGPT, два раунда):
# - перед стартом скрипт требует, чтобы на машине не было ни одного агента Ear (по имени
#   процесса KrabEarAgent, в любом каталоге/бандле, включая предков самого скрипта), ни
#   backend этого каталога, ни его штатного сокета; иначе отказ без единого kill;
# - агент определяется ИМЕНЕМ ПРОЦЕССА (pgrep -x), а не подстрокой командной строки:
#   `vim .../KrabEarAgent` или отладчик с этим путём агентом не считаются;
# - очистка и запрос завершения касаются только PID, появившихся после этой проверки,
#   только штатным TERM/terminate и без pkill по шаблону. Никакой рассылки
#   control-уведомлений (quit через NSDistributedNotificationCenter получили бы ВСЕ агенты);
# - любой сбой перечисления процессов (pgrep не 0 и не 1) означает «неизвестно»,
#   а не «остановлено»: успех завершения требует подтверждённого нуля обоих процессов
#   в одной и той же проверке;
# - фоновый старт убирается, пока он наш ПОТОМОК (проверка по ppid, а не по командной
#   строке: start_agent.command делает exec open -W и строка меняется).
# - Известный остаток: процесс, который чужой launchd поднял в окне между проверкой и нашим
#   стартом, неотличим от нашего (агент запускается через LaunchServices и не наш потомок).
#   Устранить можно только токеном запуска, который принимает агент.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT_PATH="$REPORT_DIR/smoke_release_${TS}.md"
START_LOG="$REPORT_DIR/smoke_release_${TS}.start.log"

# Только для тестов на заглушках: заменяет поиск по имени процесса шаблоном командной строки.
AGENT_PATTERN_OVERRIDE="${KRAB_SMOKE_AGENT_PATTERN_OVERRIDE:-}"
# S3/Р9: standalone active-режим (BackendSupervisor.swift) теперь спавнит
# main.py, а не backend/service.py напрямую — но старый launchd-плист/старая
# установка могли ещё гонять backend/service.py. ERE-альтернация (pgrep на
# macOS = ERE, не BRE — см. test_ensure_agent_running_contract.py) матчит оба,
# иначе смок сочтёт живой backend отсутствующим.
BACKEND_PATTERN="$ROOT_DIR/KrabEar/(backend/service|main)\.py( |$)"
# Путь каталога попадает в регулярное выражение: экранируем метасимволы ERE (скобки, точку...).
ROOT_RE="$(printf '%s' "$ROOT_DIR" | sed -E 's/[][\\.^$*+?(){}|]/\\&/g')"
BACKEND_MATCH="${ROOT_RE}${BACKEND_PATTERN#"$ROOT_DIR"}"
SOCKET_PATH="${KRAB_EAR_SOCKET:-$HOME/Library/Application Support/KrabEar/krabear.sock}"

# Таймауты ожидания; переопределяются в тестах на заглушках.
WAIT_START="${KRAB_SMOKE_WAIT_START:-15}"
WAIT_BACKEND="${KRAB_SMOKE_WAIT_BACKEND:-15}"
WAIT_STOP="${KRAB_SMOKE_WAIT_STOP:-12}"
WAIT_STOP_RETRY="${KRAB_SMOKE_WAIT_STOP_RETRY:-6}"
SETTLE_SEC="${KRAB_SMOKE_SETTLE:-2}"

# Перечисление процессов вида $1 (agent|backend). Печатает PID.
# Код: 0 — найдены, 1 — нет, 2 — pgrep не отработал («неизвестно», не «нет»).
# Остальные аргументы ($2...) — опции pgrep (например -a: включить предков на macOS).
discover() {
  local kind="$1"
  shift
  local out rc=0
  if [ "$kind" = "agent" ]; then
    if [ -n "$AGENT_PATTERN_OVERRIDE" ]; then
      out="$(pgrep "$@" -f "$AGENT_PATTERN_OVERRIDE" 2>/dev/null)" || rc=$?
    else
      out="$(pgrep "$@" -x KrabEarAgent 2>/dev/null)" || rc=$?
    fi
  else
    out="$(pgrep "$@" -f "$BACKEND_MATCH" 2>/dev/null)" || rc=$?
  fi
  case "$rc" in
    0) printf '%s\n' "$out"; return 0 ;;
    1) return 1 ;;
    *) return 2 ;;
  esac
}

# Только PID, всегда код 0 (используется там, где ошибка перечисления не должна обрывать скрипт).
pids_of() {
  discover "$1" 2>/dev/null || true
}

# Preflight ДО любых побочных действий и ДО trap очистки: ошибка здесь ничего не трогает.
if [ ! -x "$ROOT_DIR/.venv_krab_ear/bin/python" ]; then
  echo "Ошибка: .venv_krab_ear не найден. Сначала запусти Start Krab Ear.command"
  exit 1
fi

# «Есть» и «не удалось проверить» одинаково блокируют старт (fail-closed). -a: на macOS pgrep
# по умолчанию исключает предков вызывающего процесса.
preflight_blocked() {
  local rc=0
  discover "$1" -a >/dev/null || rc=$?
  [ "$rc" -ne 1 ]
}

if preflight_blocked agent || preflight_blocked backend || [ -e "$SOCKET_PATH" ]; then
  echo "Ошибка: на машине уже есть агент Krab Ear, backend этого каталога или его сокет (или pgrep недоступен)."
  echo "Smoke-check не стартует и ничего не останавливает: остановите экземпляр сами."
  exit 1
fi

mkdir -p "$REPORT_DIR"

# Запрос штатного завершения (как Cmd+Q) КАЖДОМУ нашему агенту по PID. Без рассылки.
# Успех: есть хотя бы одна цель и КАЖДЫЙ запрос принят.
terminate_own_agents() {
  local pid targets=0 failed=0
  for pid in $(pids_of agent); do
    targets=$((targets + 1))
    if ! "$ROOT_DIR/.venv_krab_ear/bin/python" - "$pid" <<'PY'
import sys
from AppKit import NSRunningApplication

app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(sys.argv[1]))
sys.exit(0 if app is not None and app.terminate() else 1)
PY
    then
      failed=$((failed + 1))
    fi
  done
  [ "$targets" -gt 0 ] && [ "$failed" -eq 0 ]
}

wait_for_kind() {
  local kind="$1"
  local timeout="$2"
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if discover "$kind" >/dev/null 2>&1; then
      remember_own_pids
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

# Подтверждённый ноль ОБОИХ процессов в одной и той же проверке. Ошибка перечисления
# («неизвестно») не считается остановкой.
both_stopped() {
  local a=0 b=0
  discover agent >/dev/null 2>&1 || a=$?
  discover backend >/dev/null 2>&1 || b=$?
  [ "$a" -eq 1 ] && [ "$b" -eq 1 ]
}

wait_until_both_stopped() {
  local timeout="$1"
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if both_stopped; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  both_stopped
}

# Число процессов вида $1 или «unknown» при сбое перечисления.
count_kind() {
  local out rc=0
  out="$(discover "$1" 2>/dev/null)" || rc=$?
  case "$rc" in
    0) printf '%s\n' "$out" | wc -l | tr -d ' ' ;;
    1) echo "0" ;;
    *) echo "unknown" ;;
  esac
}

# PID, принадлежащие этому запуску: всё, что совпало после preflight (до него совпадений
# не было, значит это наши процессы; см. известный остаток выше).
OWN_PIDS=""
START_PID=""
CLEANED_UP="no"
remember_own_pids() {
  local pid
  for pid in $(pids_of agent) $(pids_of backend); do
    case " $OWN_PIDS " in
      *" $pid "*) ;;
      *) OWN_PIDS="$OWN_PIDS $pid" ;;
    esac
  done
}

# TERM только тем запомненным PID, которые всё ещё совпадают по имени/шаблону (PID мог быть
# выдан другому процессу).
terminate_remembered() {
  local pid current
  current=" $(pids_of agent) $(pids_of backend) "
  current="${current//$'\n'/ }"
  # zsh не делит неэкранированную переменную на слова: разбиение явное, через ${=...}.
  for pid in ${=OWN_PIDS}; do
    case "$current" in
      *" $pid "*) kill "$pid" >/dev/null 2>&1 || true ;;
    esac
  done
}

cleanup() {
  # Однократно: EXIT срабатывает и после обработчика сигнала.
  [ "$CLEANED_UP" = "yes" ] && return 0
  CLEANED_UP="yes"
  local ppid_of_start killed_launcher="no"
  remember_own_pids
  terminate_remembered
  if [ -n "$START_PID" ]; then
    # Фоновый старт наш, пока мы его родитель. По командной строке нельзя: start_agent.command
    # делает exec open -W, и строка перестаёт содержать путь скрипта.
    ppid_of_start="$(ps -p "$START_PID" -o ppid= 2>/dev/null | tr -d ' ' || true)"
    if [ "$ppid_of_start" = "$$" ]; then
      kill "$START_PID" >/dev/null 2>&1 || true
      killed_launcher="yes"
    fi
  fi
  if [ "$killed_launcher" = "yes" ]; then
    # Запрос запуска мог быть уже отправлен в LaunchServices: короткое окно, чтобы
    # завершить то, что он успеет поднять.
    sleep "$SETTLE_SEC"
    remember_own_pids
    terminate_remembered
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

if wait_for_kind agent "$WAIT_START"; then
  START_OK="yes"
fi

if wait_for_kind backend "$WAIT_BACKEND"; then
  BACKEND_OK="yes"
fi

if [ "$START_OK" = "yes" ] && [ "$BACKEND_OK" = "yes" ]; then
  if terminate_own_agents; then
    CONTROL_OK="yes"
  fi
fi

if wait_until_both_stopped "$WAIT_STOP"; then
  SHUTDOWN_OK="yes"
elif [ "$START_OK" = "yes" ] && [ "$BACKEND_OK" = "yes" ]; then
  # Мягкий повтор запроса завершения и ещё немного ожидания; без fallback «не хуже baseline».
  if terminate_own_agents; then
    CONTROL_OK="yes"
  fi
  if wait_until_both_stopped "$WAIT_STOP_RETRY"; then
    SHUTDOWN_OK="yes"
  fi
fi

# Итоговая проверка: подтверждённый ноль обоих процессов именно сейчас.
if [ "$SHUTDOWN_OK" = "yes" ] && ! both_stopped; then
  SHUTDOWN_OK="no"
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
  echo "- current_agent_count: $(count_kind agent)"
  echo "- current_backend_count: $(count_kind backend)"
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
