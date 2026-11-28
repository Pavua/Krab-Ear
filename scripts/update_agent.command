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

# launchd-юнит агента (opt-in KeepAlive, install_agent_launchagent.command). Если он загружен, то после
# pkill launchd сам поднимает СТАРЫЙ бинарник ещё до установки нового, а `open` лишь активирует уже
# запущенный процесс — «PID не из OLD_PIDS» засчитал бы его как успех при старом коде (ревью Opus #2304, P2).
AGENT_LABEL="ai.krab.ear.agent"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
# Общий с verify_binaries --fix lifecycle юнита: блокировка, сверка состояния, загрузка только
# проверенного бинаря, откат из копии (ревью #2324).
source "$ROOT_DIR/scripts/agent_unit_maintenance.sh"

# Сколько секунд процессу (BSD ps: etime = [[dd-]hh:]mm:ss); пусто — неизвестно.
pid_elapsed_sec() {
  local el
  el="$("${PS_BIN:-/bin/ps}" -p "$1" -o etime= 2>/dev/null | tr -d ' ')"
  [ -n "$el" ] || return 1
  printf '%s' "$el" | awk -F'[-:]' '{ if (NF==4) print $1*86400+$2*3600+$3*60+$4; else if (NF==3) print $1*3600+$2*60+$3; else print $1*60+$2 }'
}

# Процесс стартовал не раньше метки времени $2 (epoch); неизвестный возраст = нет.
pid_started_since() {
  local el now
  el="$(pid_elapsed_sec "$1")" || return 1
  [ -n "$el" ] || return 1
  now="$(date +%s)"
  [ "$el" -le $(( now - $2 )) ]
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

# Выход в любой точке: сверка юнита с откатом (если мы его выгружали), уборка копии, снятие блокировки.
on_exit() {
  agent_unit_recover || true
  agent_backup_cleanup
  maint_lock_release
}
trap on_exit EXIT
# zsh не исполняет EXIT-trap при гибели от сигнала — превращаем сигнал в штатный exit.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Эксклюзивно с verify_binaries --fix и вторым update_agent: держим до конца загрузки юнита.
maint_lock_acquire || exit 1

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

# launchd-юнит (KeepAlive) останавливаем ВЫГРУЗКОЙ, а не pkill: иначе launchd поднимает старый бинарник
# прямо в окне cp → mv → codesign и может стартовать бинарь с ещё не обновлённой подписью (сбой TCC/cdhash).
# Процедура та же, что ручная пересборка 27.11: bootout → подмена → подпись → проверка → bootstrap.
# Busy-гейт выше — последняя проверка перед bootout (между ними только launchctl print и копия бинарей);
# quiesce-рукопожатие с backend (запрет старта записи на время обслуживания) — вне объёма.
UNIT_MANAGED=0
UNIT_STATE="$(agent_unit_state)" || preflight_fail "состояние launchd-юнита $AGENT_LABEL неизвестно — агент не остановлен"
if [ "$UNIT_STATE" = loaded ]; then
  UNIT_MANAGED=1
  # Без plist юнит потом не вернуть — ничего не останавливаем.
  [ -f "$AGENT_PLIST" ] || preflight_fail "юнит $AGENT_LABEL загружен, но нет $AGENT_PLIST — агент не остановлен"
fi

# Копия прежних бинарей до любых изменений: из неё откат, если новая подпись не пройдёт проверку.
agent_backup_binaries || preflight_fail "не удалось сохранить резервную копию бинарей — агент не остановлен"

if [ "$UNIT_MANAGED" -eq 1 ]; then
  echo "launchd-юнит $AGENT_LABEL загружен — выгружаю (bootout) до подмены бинаря."
  # Флаг ДО bootout: сигнал посреди выгрузки тоже ведёт к сверке реального состояния на выходе.
  UNIT_RECOVERY=1
  if ! "${LAUNCHCTL_BIN:-launchctl}" bootout "gui/$(id -u)/$AGENT_LABEL"; then
    echo "Ошибка: launchctl bootout $AGENT_LABEL не удался — бинарники не тронуты."
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
if ! agent_sign_and_verify; then
  # Юнит под launchd вернёт on_exit — только после отката и проверенной подписи.
  echo "Ошибка: новый бинарь не прошёл подпись/проверку (codesign --verify --strict)."
  exit 1
fi

# Запускаем через LaunchServices (`open`) — macOS создаёт управляемый
# application.com.antigravity.krab-ear.* job, тот же самый, что появляется при
# клике иконки в Dock. Это гарантирует отсутствие дубликата в Dock и
# отсутствие orphan-процесса `com.apple.xpc.launchd.unmanaged.KrabEarAgent.*`,
# который раньше появлялся при backgrounded-exec из shell.
INSTALL_EPOCH="$(date +%s)"
if [ "$UNIT_MANAGED" -eq 1 ]; then
  # Юнит под launchd: `open` поверх него даёт второй экземпляр (шторм agent.lock) — возвращаем юнит.
  echo "Загружаю launchd-юнит $AGENT_LABEL с новым бинарём (bootstrap)."
  if ! agent_unit_bootstrap; then
    UNIT_RECOVERY=0  # попытки исчерпаны (две); on_exit не повторяет
    echo "Ошибка: launchctl bootstrap $AGENT_LABEL не удался — бинарники установлены и подписаны, агент не запущен."
    agent_unit_manual_hint "launchctl bootstrap не удался"
    exit 1
  fi
  UNIT_RECOVERY=0  # загрузка подтверждена launchctl print
else
  open "$APP_BUNDLE"
fi

# Успех — процесс, которого не было до остановки; под launchd — ещё и стартовавший ПОСЛЕ установки:
# поднятый до неё старый экземпляр не выдаёт себя за новый.
started=0
for _ in {1..60}; do
  for cand in $(pgrep -f "$AGENT_PATTERN_BUNDLE" 2>/dev/null || true); do
    case " $OLD_PIDS " in
      *" $cand "*) continue ;;
    esac
    if [ "$UNIT_MANAGED" -eq 1 ] && ! pid_started_since "$cand" "$INSTALL_EPOCH"; then continue; fi
    started=1; break
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
