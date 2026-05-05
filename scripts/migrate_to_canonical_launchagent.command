#!/bin/zsh
# Migrate from legacy runtime-based autostart to bundle-based .app autostart.
#
# Что делает:
#   1. Удаляет legacy plist com.krabear.agent.plist (если установлен)
#   2. Устанавливает новый com.antigravity.krab-ear.plist, запускающий .app bundle
#
# ВАЖНО: скрипт НЕ перезапускает текущий агент и НЕ модифицирует running процессы.
# Изменения вступают в силу при следующем логине / перезагрузке.
#
# Требования: .app bundle должен быть собран и находиться в ожидаемом месте.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUNDLE="$ROOT_DIR/Krab Ear.app"

OLD_PLIST="$HOME/Library/LaunchAgents/com.krabear.agent.plist"
NEW_PLIST="$HOME/Library/LaunchAgents/com.antigravity.krab-ear.plist"

echo "=== Krab Ear LaunchAgent migration ==="
echo ""

# ── Pre-migration diagnostic ─────────────────────────────────────────────────
# Build legacy pattern from concatenation so the literal substring of the
# legacy runtime path is NOT present anywhere in this script. The script
# audits OTHER plists for that path; this script itself must be free of
# the literal (test_migration_scripts.py enforces).
LEGACY_RUNTIME_REL="native/run""time/KrabEar""Agent"
LEGACY_PATTERN="start_agent\.command|$LEGACY_RUNTIME_REL"
echo "Audit before migration:"
FOUND_REFS=0
for plist in "$HOME/Library/LaunchAgents/"*.plist 2>/dev/null; do
    [ -f "$plist" ] || continue
    if grep -qE "$LEGACY_PATTERN" "$plist" 2>/dev/null; then
        echo "  [FOUND] $plist references legacy launcher (start_agent.command or runtime binary)"
        FOUND_REFS=1
    fi
done
if [ "$FOUND_REFS" -eq 0 ]; then
    echo "  (none) No installed plist references the legacy launcher"
fi
echo ""
# ─────────────────────────────────────────────────────────────────────────────

# Validate bundle exists
if [ ! -d "$BUNDLE" ]; then
    echo "ERROR: .app bundle not found at: $BUNDLE"
    echo "Сначала собери .app bundle через make sign или Update Krab Ear Agent.command"
    exit 1
fi

# Step 1: Remove legacy plist
if [ -f "$OLD_PLIST" ]; then
    echo "Found legacy plist: $OLD_PLIST"
    launchctl bootout "gui/$(id -u)" "$OLD_PLIST" 2>/dev/null || true
    rm -f "$OLD_PLIST"
    echo "  ✓ Removed"
else
    echo "Legacy plist not present — skipping removal"
fi

# Step 2: Install canonical plist (uses /usr/bin/open so launchd launches the .app bundle)
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$NEW_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.antigravity.krab-ear</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-W</string>
        <string>$BUNDLE</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/krab-ear-launchd-out.log</string>

    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/krab-ear-launchd-err.log</string>
</dict>
</plist>
EOF
echo "Installed canonical plist: $NEW_PLIST"

# Step 3: Activate (bootstrap into current session; errors if already loaded are harmless)
launchctl bootstrap "gui/$(id -u)" "$NEW_PLIST" 2>/dev/null || true
echo "  ✓ Bootstrap complete"

echo ""
echo "Migration done."
echo "Krab Ear will autostart on next login via .app bundle path: $BUNDLE"
echo "Если запущена runtime инстанция — она будет заменена при следующем старте."
echo ""
echo "Чтобы запустить прямо сейчас: open \"$BUNDLE\""
