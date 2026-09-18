#!/bin/bash
# Install/reinstall ai.krab.ear.reliability LaunchAgent (R1 scanner).
#
# Что делает:
#   1. Читает template `KrabEar/launchagents/ai.krab.ear.reliability.plist.template`
#   2. Подставляет __HOME__, __PROJECT_ROOT__
#   3. plutil -lint до bootstrap (fail-closed на битом XML)
#   4. bootout старой версии (если есть) → записывает plist → bootstrap
#   5. Verify: launchctl print показывает StartCalendarInterval Hour=6 Minute=0
#   6. Один ручной `--once` прогон (пишет снимок)
#
# Безопасен для прода: job scan-only (только чтение источников), RunAtLoad=false,
# Background + LowPriorityIO. Чужие launchd-юниты НЕ трогаются.
# Секретов в plist нет.
#
# macOS Bash 3.2: нет mapfile/assoc — только while-read и sed.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT_DIR/KrabEar/launchagents/ai.krab.ear.reliability.plist.template"
TARGET="$HOME/Library/LaunchAgents/ai.krab.ear.reliability.plist"
LABEL="ai.krab.ear.reliability"
UID_NUM="$(id -u)"

log() { printf '[install-reliability] %s\n' "$*"; }
fail() { printf '[install-reliability] ❌ %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || fail "template not found: $TEMPLATE"
[ -f "$ROOT_DIR/scripts/reliability_scan.py" ] || fail "scanner not found"
[ -x "$ROOT_DIR/.venv_krab_ear/bin/python3" ] || fail "venv python not found: $ROOT_DIR/.venv_krab_ear/bin/python3"

mkdir -p "$ROOT_DIR/logs"

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

# StartCalendarInterval launchd рендерит как "Hour" => 6 / "Minute" => 0.
# 🔴 grep «run interval» из e2e-smoke тут НЕ подходит (там StartInterval).
DESC="$(launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null || true)"
if printf '%s' "$DESC" | grep -q '"Hour" => 6' && printf '%s' "$DESC" | grep -q '"Minute" => 0'; then
  log "✅ installed: $LABEL (StartCalendarInterval 06:00, RunAtLoad=false)"
else
  printf '%s\n' "$DESC" | head -30 || true
  fail "verify failed: Hour=6 / Minute=0 not found"
fi

log "ручной --once прогон (пишет снимок)..."
"$ROOT_DIR/.venv_krab_ear/bin/python3" -u "$ROOT_DIR/scripts/reliability_scan.py" --once \
  || fail "manual --once run failed"
log "✅ снимок записан: $HOME/.local/share/krab-ear/reliability/latest.json"
