#!/bin/zsh
# ------------------------------------------------------------------
# DEPRECATED: Установка launchd автозапуска Krab Ear Agent через
# standalone-бинарь (com.krabear.agent.plist → start_agent.command →
# native/runtime/KrabEarAgent). Этот путь вызывал появление дубликата
# иконки в Dock, т.к. launchd при бэкграунд-форке породителей создавал
# orphan-процесс `com.apple.xpc.launchd.unmanaged.KrabEarAgent.*`,
# сосуществующий с .app bundle-инстансом.
#
# Вместо этого используйте autostart через LaunchServices и .app bundle:
# см. `Enable Krab Ear Autostart.command` в корне проекта и
# `LaunchAgentManager.swift` (который ставит бандл-ориентированный
# LaunchAgent). Подробности: PR "fix(scripts): launch .app via
# LaunchServices to prevent duplicate agent process".
#
# Этот скрипт оставлен для обратной совместимости dormant-сценариев и
# должен быть удалён после очередной чистки.
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
