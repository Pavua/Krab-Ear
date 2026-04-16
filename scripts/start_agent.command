#!/bin/zsh
# ------------------------------------------------------------------
# Единый запуск нативного Krab Ear Agent (Swift + Python backend).
# Запускать двойным кликом или через launchd.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/KrabEar"
REQ_FILE="$APP_DIR/requirements.txt"
VENV_DIR="$ROOT_DIR/.venv_krab_ear"
STAMP_FILE="$VENV_DIR/.requirements.sha256"
PACKAGE_DIR="$ROOT_DIR/native/KrabEarAgent"
AGENT_BIN="$PACKAGE_DIR/.build/release/KrabEarAgent"
RUNTIME_DIR="$ROOT_DIR/native/runtime"
AGENT_RUNTIME_BIN="$RUNTIME_DIR/KrabEarAgent"
LEGACY_AGENT_BIN="$ROOT_DIR/native/KrabEarAgent/.build/release/KrabEarAgent"

SHOW_HISTORY=0
LAUNCHED_BY_LAUNCHD=0
FORCE_REBUILD=0
EXTRA_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --show-history)
      SHOW_HISTORY=1
      EXTRA_ARGS+=("$arg")
      ;;
    --launched-by-launchd)
      LAUNCHED_BY_LAUNCHD=1
      EXTRA_ARGS+=("$arg")
      ;;
    --force-rebuild)
      FORCE_REBUILD=1
      ;;
    *)
      EXTRA_ARGS+=("$arg")
      ;;
  esac
done

log() {
  if [ "$LAUNCHED_BY_LAUNCHD" -eq 0 ]; then
    echo "$1"
  fi
}

running_agent_pids() {
  # Ищем только процессы текущего проекта, чтобы не трогать другие окружения.
  local runtime_pattern="$AGENT_RUNTIME_BIN --project-root $ROOT_DIR"
  local legacy_pattern="$LEGACY_AGENT_BIN --project-root $ROOT_DIR"
  {
    pgrep -f "$runtime_pattern" 2>/dev/null || true
    pgrep -f "$legacy_pattern" 2>/dev/null || true
  } | awk 'NF { print $1 }' | awk '!seen[$1]++'
}

wait_until_agent_stops() {
  local attempts="$1"
  local delay_sec="$2"
  local idx=0
  while [ "$idx" -lt "$attempts" ]; do
    if [ -z "$(running_agent_pids)" ]; then
      return 0
    fi
    sleep "$delay_sec"
    idx=$((idx + 1))
  done
  return 1
}

stop_running_agent_for_rebuild() {
  local pids
  pids="$(running_agent_pids)"
  [ -n "$pids" ] || return 0

  log "Обнаружен запущенный агент: $pids"
  log "Запрошен --force-rebuild: останавливаю агент перед пересборкой"
  send_control_action "quit" || true
  if wait_until_agent_stops 16 0.25; then
    return 0
  fi

  log "Агент не завершился сам, отправляю SIGTERM"
  while IFS= read -r pid; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done <<< "$pids"
  if wait_until_agent_stops 12 0.25; then
    return 0
  fi

  pids="$(running_agent_pids)"
  if [ -n "$pids" ]; then
    log "Агент всё ещё активен, отправляю SIGKILL"
    while IFS= read -r pid; do
      [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
    done <<< "$pids"
    wait_until_agent_stops 6 0.2 || true
  fi
}

send_control_action() {
  local action="$1"
  "$VENV_DIR/bin/python" - "$action" <<'PY'
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

if [ ! -d "$APP_DIR" ]; then
  echo "Ошибка: папка KrabEar не найдена: $APP_DIR"
  exit 1
fi

if [ ! -f "$REQ_FILE" ]; then
  echo "Ошибка: файл зависимостей не найден: $REQ_FILE"
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  BASE_PYTHON="$(command -v python3 || true)"
  if [ -z "$BASE_PYTHON" ]; then
    echo "Ошибка: python3 не найден в PATH"
    exit 1
  fi
  log "Создаю venv: $VENV_DIR"
  "$BASE_PYTHON" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null

REQ_HASH="$(shasum -a 256 "$REQ_FILE" | awk '{print $1}')"
INSTALLED_HASH="$(cat "$STAMP_FILE" 2>/dev/null || true)"
if [ "$REQ_HASH" != "$INSTALLED_HASH" ]; then
  log "Устанавливаю/обновляю Python-зависимости Krab Ear"
  python -m pip install -r "$REQ_FILE"
  echo "$REQ_HASH" > "$STAMP_FILE"
fi

if [ -n "$(running_agent_pids)" ]; then
  if [ "$FORCE_REBUILD" -eq 1 ]; then
    stop_running_agent_for_rebuild
  else
    if [ "$SHOW_HISTORY" -eq 1 ]; then
      send_control_action "show_history" || true
      log "Команда show_history отправлена работающему агенту"
    else
      log "Krab Ear Agent уже запущен"
    fi
    exit 0
  fi
fi

NEED_BUILD=0
if [ "$FORCE_REBUILD" -eq 1 ] || [ ! -x "$AGENT_BIN" ]; then
  NEED_BUILD=1
fi

if [ "$NEED_BUILD" -eq 1 ]; then
  log "Собираю нативный агент (Swift release)"
  swift build -c release --package-path "$PACKAGE_DIR"
fi

mkdir -p "$RUNTIME_DIR"
NEED_RUNTIME_SYNC=0
if [ "$NEED_BUILD" -eq 1 ] || [ ! -x "$AGENT_RUNTIME_BIN" ]; then
  NEED_RUNTIME_SYNC=1
else
  BUILD_SHA="$(shasum -a 256 "$AGENT_BIN" | awk '{print $1}')"
  RUNTIME_SHA="$(shasum -a 256 "$AGENT_RUNTIME_BIN" | awk '{print $1}')"
  if [ "$BUILD_SHA" != "$RUNTIME_SHA" ]; then
    NEED_RUNTIME_SYNC=1
  fi
fi

if [ "$NEED_RUNTIME_SYNC" -eq 1 ]; then
  cp "$AGENT_BIN" "$AGENT_RUNTIME_BIN"
  chmod +x "$AGENT_RUNTIME_BIN"
  # На Apple Silicon исполняемый бинарь должен быть подписан.
  # Подписываем только при реальном обновлении бинаря, чтобы не провоцировать
  # лишние изменения записи в TCC/Accessibility.
  codesign --force --sign - --timestamp=none --identifier com.antigravity.krab-ear "$AGENT_RUNTIME_BIN" >/dev/null 2>&1 || true
fi

# Backend launchd job: bootstrap, если plist установлен но не загружен в launchd.
# launchd с KeepAlive=true мгновенно (~1с) респавнит backend при любой смерти,
# что устраняет «GUI висит, hotkey не работает». Если plist отсутствует —
# Swift agent сам поднимет backend через ad-hoc Process() (legacy fallback).
BACKEND_PLIST="$HOME/Library/LaunchAgents/ai.krab.ear.backend.plist"
if [ -f "$BACKEND_PLIST" ]; then
  UID_NUM="$(id -u)"
  if ! launchctl print "gui/$UID_NUM/ai.krab.ear.backend" >/dev/null 2>&1; then
    log "Bootstrap backend launchd job (ai.krab.ear.backend)"
    launchctl bootstrap "gui/$UID_NUM" "$BACKEND_PLIST" 2>&1 | grep -v "already loaded" || true
  fi
fi

export KRAB_EAR_PROJECT_ROOT="$ROOT_DIR"
log "Запускаю Krab Ear Agent"
exec "$AGENT_RUNTIME_BIN" --project-root "$ROOT_DIR" "${EXTRA_ARGS[@]}"
