#!/bin/zsh
# ------------------------------------------------------------------
# Установка launchd автозапуска Krab Ear Agent.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/com.krabear.agent.plist"
START_SCRIPT="$ROOT_DIR/scripts/start_agent.command"
LOG_DIR="$HOME/Library/Logs"

mkdir -p "$LAUNCH_AGENTS_DIR"
mkdir -p "$LOG_DIR"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.krabear.agent</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$START_SCRIPT</string>
    <string>--launched-by-launchd</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/KrabEarAgent.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/KrabEarAgent.error.log</string>
</dict>
</plist>
PLIST

chmod 644 "$PLIST_PATH"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_PATH"
launchctl kickstart -k "gui/$UID_NUM/com.krabear.agent" >/dev/null 2>&1 || true

echo "✅ Автозапуск Krab Ear Agent установлен"
echo "Plist: $PLIST_PATH"
echo "Логи:  $LOG_DIR/KrabEarAgent.log"
