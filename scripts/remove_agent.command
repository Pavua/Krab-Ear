#!/bin/zsh
# ------------------------------------------------------------------
# Удаление launchd автозапуска и остановка Krab Ear Agent.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.krabear.agent.plist"
AGENT_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

if [ -x "$AGENT_BIN" ]; then
  pkill -f "$AGENT_BIN --project-root $ROOT_DIR" >/dev/null 2>&1 || true
fi
pkill -f "$ROOT_DIR/KrabEar/backend/service.py" >/dev/null 2>&1 || true

echo "✅ Krab Ear Agent остановлен и удалён из автозапуска"
