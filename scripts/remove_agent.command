#!/bin/zsh
# ------------------------------------------------------------------
# Удаление launchd автозапуска и остановка Krab Ear Agent.
#
# Остановка backend теряет аудио активной записи (оно живёт в памяти
# процесса), поэтому перед ЛЮБЫМ изменением скрипт спрашивает
# safe_backend_restart.command --check-only (запись/встреча/неизвестно →
# отказ, код 1). `--force` — осознанный обход гейта.
# ------------------------------------------------------------------

set -euo pipefail

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.krabear.agent.plist"
BACKEND_PLIST_PATH="$HOME/Library/LaunchAgents/ai.krab.ear.backend.plist"
AGENT_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"

UID_NUM="$(id -u)"
AGENT_PLIST_PATH="$HOME/Library/LaunchAgents/ai.krab.ear.agent.plist"

# 0. Busy-гейт: до любого bootout/pkill/rm.
if [ "$FORCE" -ne 1 ]; then
  if ! "$ROOT_DIR/scripts/safe_backend_restart.command" --check-only >/dev/null 2>&1; then
    echo "❌ Backend занят записью/встречей либо его состояние неизвестно — ничего не остановлено." >&2
    echo "   Дождитесь конца записи и повторите, либо запустите с --force (аудио активной записи будет потеряно)." >&2
    exit 1
  fi
fi

# 1. Старый автозапуск Swift агента (legacy plist)
launchctl bootout "gui/$UID_NUM" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

# 1b. Актуальный LaunchAgent агента (install_agent_launchagent.command, KeepAlive):
#     без bootout launchd вернул бы агента сразу после pkill.
launchctl bootout "gui/$UID_NUM/ai.krab.ear.agent" >/dev/null 2>&1 || true
rm -f "$AGENT_PLIST_PATH"

# 2. Backend launchd job — bootout ОБЯЗАТЕЛЬНО до pkill, иначе launchd
#    мгновенно (за ~1 сек) респавнит процесс и pkill становится бесполезен.
if [ -f "$BACKEND_PLIST_PATH" ]; then
  launchctl bootout "gui/$UID_NUM/ai.krab.ear.backend" >/dev/null 2>&1 || true
fi

# 3. Swift агент (не launchd, обычный pkill ок)
if [ -x "$AGENT_BIN" ]; then
  pkill -f "$AGENT_BIN --project-root $ROOT_DIR" >/dev/null 2>&1 || true
fi

# 4. Подстраховка: если backend всё ещё жив (например запущен вне launchd),
#    добиваем pkill. После bootout launchd уже не станет респавнить.
#    S3/Р9: плист теперь запускает main.py, а не backend/service.py напрямую
#    (test_backend_plist_data_dir_parity_S3.py) — но до переустановки юнита
#    или в standalone active-режиме процесс мог стартовать под ЛЮБЫМ именем.
#    ERE-альтернация (pkill/pgrep на macOS = ERE, не BRE — см.
#    test_ensure_agent_running_contract.py) матчит оба.
pkill -f "$ROOT_DIR/KrabEar/(backend/service|main)\.py" >/dev/null 2>&1 || true

echo "✅ Krab Ear Agent остановлен и удалён из автозапуска"
