#!/bin/zsh
# ------------------------------------------------------------------
# Явное обновление нативного агента Krab Ear:
# 1) форс-сборка Swift;
# 2) синхронизация runtime-бинаря;
# 3) перезапуск агента.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"
APP_BUNDLE="$ROOT_DIR/Krab Ear.app"
APP_BUNDLE_BIN="$APP_BUNDLE/Contents/MacOS/KrabEarAgent"
PACKAGE_DIR="$ROOT_DIR/native/KrabEarAgent"
BUILD_BIN="$PACKAGE_DIR/.build/release/KrabEarAgent"
BUNDLE_ID="com.antigravity.krab-ear"
# Матчим оба пути: .app bundle (рекомендуемый LaunchServices-путь) и legacy standalone runtime.
AGENT_PATTERN_BUNDLE="$APP_BUNDLE_BIN"
AGENT_PATTERN_LEGACY="$RUNTIME_BIN --project-root $ROOT_DIR"
VENV_PY="$ROOT_DIR/.venv_krab_ear/bin/python"
SETTINGS_PATH="$HOME/Library/Application Support/KrabEar/settings.json"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      echo "Usage: $0 [--force]"
      echo "  --force  не проверять активную запись/встречу (аудио активной записи будет потеряно)"
      exit 0 ;;
    *) echo "unknown arg: $arg" >&2; echo "Usage: $0 [--force]" >&2; exit 2 ;;
  esac
done

# Процесс жив и не зомби.
pid_alive() {
  local st
  st="$(/bin/ps -p "$1" -o stat= 2>/dev/null || true)"
  [ -n "$st" ] && [ "${st#Z}" = "$st" ]
}

preflight_fail() {
  echo "Ошибка preflight: $1"
  exit 1
}

require_cmd() {
  local name="$1"
  command -v "$name" >/dev/null 2>&1 || preflight_fail "не найдено: $name"
}

check_free_space_mb() {
  local file_path="$1"
  local min_mb="$2"
  local free_kb
  free_kb="$(/bin/df -k "$file_path" | /usr/bin/awk 'NR==2 {print $4}')"
  [ -n "$free_kb" ] || preflight_fail "не удалось определить свободное место"
  local free_mb=$((free_kb / 1024))
  if [ "$free_mb" -lt "$min_mb" ]; then
    preflight_fail "недостаточно места: ${free_mb}MB (нужно >= ${min_mb}MB)"
  fi
}

require_cmd swift
require_cmd pgrep
require_cmd codesign
require_cmd open
# Недоступный/отозванный сертификат не должен остановить живой агент.
source "$ROOT_DIR/scripts/agent_signing_identity.sh"
SIGN_ID="$(resolve_agent_signing_identity)" || exit 1
[ -x "$VENV_PY" ] || preflight_fail "не найден python venv: $VENV_PY"
[ -d "$ROOT_DIR/native/runtime" ] || mkdir -p "$ROOT_DIR/native/runtime"
[ -w "$ROOT_DIR/native/runtime" ] || preflight_fail "нет прав записи в $ROOT_DIR/native/runtime"
[ -w "$ROOT_DIR" ] || preflight_fail "нет прав записи в $ROOT_DIR"
[ -d "$APP_BUNDLE" ] || preflight_fail "не найден .app bundle: $APP_BUNDLE"
[ -w "$APP_BUNDLE/Contents/MacOS" ] || preflight_fail "нет прав записи в $APP_BUNDLE/Contents/MacOS"
check_free_space_mb "$ROOT_DIR" 200

UPDATE_CHANNEL="stable"
if [ -f "$SETTINGS_PATH" ]; then
  UPDATE_CHANNEL="$("$VENV_PY" - <<'PY' "$SETTINGS_PATH"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
try:
    payload=json.loads(p.read_text(encoding='utf-8'))
except Exception:
    print("stable")
    raise SystemExit(0)
value=str(payload.get("update_channel","stable")).strip().lower()
print(value if value in {"stable","beta"} else "stable")
PY
)"
fi

echo "Обновляю Krab Ear Agent... (channel: $UPDATE_CHANNEL)"

# Сборка — ДО остановки агента: упавший `swift build` не должен оставлять машину
# без работающего агента (хоткей, вставка) и со старым бинарём.
echo "Собираю нативный агент (Swift release)"
swift build -c release --package-path "$PACKAGE_DIR"

[ -x "$BUILD_BIN" ] || preflight_fail "build artifact не найден: $BUILD_BIN"

# Запоминаем PID работающего агента (как .app bundle, так и legacy standalone).
OLD_PIDS="$( { pgrep -f "$AGENT_PATTERN_BUNDLE" 2>/dev/null || true; pgrep -f "$AGENT_PATTERN_LEGACY" 2>/dev/null || true; } | sort -u | tr '\n' ' ')"

# Busy-гейт до остановки (ревью Opus #2304, P2; как в remove_agent/repair_permissions): остановка агента
# посреди диктовки/встречи теряет аудио. Состояние неизвестно = отказ; без работающего агента терять нечего.
if [ -n "${OLD_PIDS// /}" ] && [ "$FORCE" -ne 1 ]; then
  if ! "$ROOT_DIR/scripts/safe_backend_restart.command" --check-only >/dev/null 2>&1; then
    echo "Backend занят записью/встречей либо его состояние неизвестно — ничего не остановлено."
    echo "Дождитесь конца записи и повторите, либо запустите с --force (аудио активной записи будет потеряно)."
    exit 1
  fi
fi

# Без остановки pkill после rebuild обновление бинаря приведёт к сбою TCC cdhash и "висящему" экземпляру.
pkill -f "$AGENT_PATTERN_BUNDLE" 2>/dev/null || true
pkill -f "$AGENT_PATTERN_LEGACY" 2>/dev/null || true
# Ждём смерти именно СТАРЫХ PID (под launchd KeepAlive новый процесс поднимается сам — это не помеха).
# Не вышли — SIGKILL не шлём и бинарники не подменяем: иначе «успех» дал бы ещё живой старый процесс.
old_gone=0
for _ in {1..100}; do
  alive=0
  for pid in $(printf '%s' "$OLD_PIDS"); do  # разбиение по пробелам: одинаково в zsh и bash
    if pid_alive "$pid"; then alive=1; fi
  done
  if [ "$alive" -eq 0 ]; then old_gone=1; break; fi
  sleep 0.1
done
if [ "$old_gone" -ne 1 ]; then
  echo "Ошибка: прежний агент не завершился по SIGTERM за 10 с — бинарники не тронуты (SIGKILL не посылаю: он мог бы оборвать запись)."
  exit 1
fi

# Синхронизируем свежесобранный бинарь в обе точки доставки:
#   * native/runtime/KrabEarAgent — legacy путь (dev-сценарии, smoke-тесты);
#   * Krab Ear.app/Contents/MacOS/KrabEarAgent — production, запускаемый через LaunchServices.
# Ревью G2-Krab-Ear-02b2, KE02B2-02/13: копия идёт во временный файл рядом и встаёт на
# место атомарным `mv` (обрыв посреди `cp -f` оставлял усечённый бинарник в .app), а
# destination-symlink отклоняется (`cp -f` пошёл бы по ссылке в чужой файл).
install_binary() {
  local src="$1" dest="$2" tmp
  if [ -L "$dest" ]; then
    echo "Ошибка: $dest — символическая ссылка, поставка отклонена"
    exit 1
  fi
  tmp="${dest}.new.$$"
  cp "$src" "$tmp" || { rm -f "$tmp"; echo "Ошибка: копирование в $tmp не удалось"; exit 1; }
  chmod +x "$tmp"
  mv -f "$tmp" "$dest" || { rm -f "$tmp"; echo "Ошибка: установка $dest не удалась"; exit 1; }
}
install_binary "$BUILD_BIN" "$RUNTIME_BIN"
install_binary "$BUILD_BIN" "$APP_BUNDLE_BIN"

echo "Подписываю стабильным сертификатом: $SIGN_ID"
codesign --force --sign "$SIGN_ID" --timestamp=none --identifier "$BUNDLE_ID" "$RUNTIME_BIN"
codesign --force --sign "$SIGN_ID" --timestamp=none --identifier "$BUNDLE_ID" "$APP_BUNDLE"
codesign --verify --strict "$RUNTIME_BIN"
codesign --verify --strict "$APP_BUNDLE"

# Запускаем через LaunchServices (`open`) — macOS создаёт управляемый
# application.com.antigravity.krab-ear.* job, тот же самый, что появляется при
# клике иконки в Dock. Это гарантирует отсутствие дубликата в Dock и
# отсутствие orphan-процесса `com.apple.xpc.launchd.unmanaged.KrabEarAgent.*`,
# который раньше появлялся при backgrounded-exec из shell.
open "$APP_BUNDLE"

# Успех — процесс с PID, которого не было до остановки: старый экземпляр не выдаёт себя за новый.
started=0
for _ in {1..60}; do
  for cand in $(pgrep -f "$AGENT_PATTERN_BUNDLE" 2>/dev/null || true); do
    case " $OLD_PIDS " in
      *" $cand "*) ;;
      *) started=1; break ;;
    esac
  done
  [ "$started" -eq 1 ] && break
  sleep 0.25
done

if [ "$started" -eq 1 ]; then
  echo "Готово. Агент обновлён и запущен."
else
  # KE02B2-11: «open отработал» ≠ «агент поднялся». Без процесса — ошибка, а не
  # предупреждение с кодом 0: вызывающий (цикл волн, CI-обвязка) иначе считает
  # обновление успешным при мёртвом агенте.
  echo "Ошибка: процесс агента не появился за 15 с после open. Проверьте лог: ~/Library/Application Support/KrabEar/agent.log"
  exit 1
fi
