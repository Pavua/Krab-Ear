#!/bin/bash
# Install/reinstall ai.krab.ear.e2e-smoke LaunchAgent (Ear E2E smoke, R4).
#
# Что делает:
#   1. Читает template `KrabEar/launchagents/ai.krab.ear.e2e-smoke.plist.template`
#   2. Подставляет __HOME__, __PROJECT_ROOT__
#   3. plutil -lint до bootstrap (fail-closed на битом XML)
#   4. bootout старой версии (если есть) → записывает plist → bootstrap
#   5. Verify: launchctl print показывает job + StartInterval 21600
#
# Безопасен для прода: job read-only (никаких record/dictation),
# RunAtLoad=false (первый прогон через 6 ч), Background + LowPriorityIO.
# Секретов в plist нет (токен Sentry скрипт читает из Main Krab .env в runtime).
#
# macOS Bash 3.2: нет mapfile/assoc — только while-read и sed.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT_DIR/KrabEar/launchagents/ai.krab.ear.e2e-smoke.plist.template"
TARGET="$HOME/Library/LaunchAgents/ai.krab.ear.e2e-smoke.plist"
LABEL="ai.krab.ear.e2e-smoke"
UID_NUM="$(id -u)"

log() { printf '[install-smoke] %s\n' "$*"; }
fail() { printf '[install-smoke] ❌ %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || fail "template not found: $TEMPLATE"
[ -f "$ROOT_DIR/scripts/ear_e2e_smoke.py" ] || fail "smoke script not found"

# Экранирование & для sed replacement (в путях с пробелами & редок, но дёшево).
esc() { printf '%s' "$1" | sed 's/[&]/\\&/g'; }
HOME_ESC="$(esc "$HOME")"
ROOT_ESC="$(esc "$ROOT_DIR")"

sed -e "s|__HOME__|$HOME_ESC|g" -e "s|__PROJECT_ROOT__|$ROOT_ESC|g" \
  "$TEMPLATE" > "$TARGET" || fail "render failed"

plutil -lint "$TARGET" >/dev/null || fail "plutil -lint failed: $TARGET"
log "plist rendered + lint OK: $TARGET"

wait_for_bootout() {
  local waited=0
  while launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; do
    if [ "$waited" -ge 10 ]; then
      log "⚠️ bootout не подтверждён за 10с — продолжаю"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  log "bootout previous version..."
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  wait_for_bootout || true
fi

log "bootstrap..."
launchctl bootstrap "gui/$UID_NUM" "$TARGET" || fail "bootstrap failed"

# launchctl print рендерит StartInterval как "run interval = N seconds".
if launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -q "run interval = 21600 seconds"; then
  log "✅ installed: $LABEL (StartInterval 21600, first run in ~6h)"
else
  launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | head -20 || true
  fail "verify failed: run interval 21600 not found"
fi
