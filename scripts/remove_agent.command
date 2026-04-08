#!/bin/zsh
# ------------------------------------------------------------------
# Удаление launchd автозапуска и остановка Krab Ear Agent.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.krabear.agent.plist"
BACKEND_PLIST_PATH="$HOME/Library/LaunchAgents/ai.krab.ear.backend.plist"
AGENT_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"

UID_NUM="$(id -u)"

# 1. Старый автозапуск Swift агента (legacy plist)
launchctl bootout "gui/$UID_NUM" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

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
pkill -f "$ROOT_DIR/KrabEar/backend/service.py" >/dev/null 2>&1 || true

echo "✅ Krab Ear Agent остановлен и удалён из автозапуска"
