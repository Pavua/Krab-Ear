#!/bin/bash
# Wave 59 — Install/reinstall ai.krab.ear.agent LaunchAgent (Swift agent self-recovery).
#
# Что делает:
#   1. Читает template KrabEar/launchagents/ai.krab.ear.agent.plist.template
#   2. Подставляет __PROJECT_ROOT__, __AGENT_BINARY__ (путь к .app/Contents/MacOS/KrabEarAgent)
#   3. bootout старой версии (если есть) → записывает plist → bootstrap
#   4. Smoke test: ждёт process в pgrep до 10 секунд
#
# Зачем: Phase A supervisor пингует backend каждые 3s. Но если сам Swift agent
# умер — некому пинговать. 12h audit показал 04:07Z = «Swift agent 0 procs»,
# auto-recovery только через user activity 6h спустя. launchd для agent label
# respawn'ит за ThrottleInterval=10s независимо от причины смерти.
#
# Opt-in: user сам решает использовать ли launchd для agent (некоторые ставят
# через Login Items вместо launchd — это альтернатива).

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT_DIR/KrabEar/launchagents/ai.krab.ear.agent.plist.template"
TARGET="$HOME/Library/LaunchAgents/ai.krab.ear.agent.plist"
LABEL="ai.krab.ear.agent"
AGENT_BINARY="$ROOT_DIR/Krab Ear.app/Contents/MacOS/KrabEarAgent"
UID_NUM="$(id -u)"

log() { printf '[install-agent] %s\n' "$*"; }
fail() { printf '[install-agent] ❌ %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || fail "template not found: $TEMPLATE"
[ -x "$AGENT_BINARY" ] || fail "agent binary не найден или не исполняемый: $AGENT_BINARY (запусти make sign сначала)"
mkdir -p "$ROOT_DIR/logs"
mkdir -p "$HOME/Library/LaunchAgents"

# 1. Bootout старой версии (если загружена)
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  log "bootout gui/$UID_NUM/$LABEL (была загружена старая версия)"
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>&1 || true
  sleep 1
fi

# 2. Render template. Используем python для безопасного replace (пути могут
# содержать пробелы — например "Krab Ear.app").
log "render template → $TARGET"
python3 -c "
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()
src = src.replace('__PROJECT_ROOT__', sys.argv[2])
src = src.replace('__AGENT_BINARY__', sys.argv[3])
pathlib.Path(sys.argv[4]).write_text(src)
" "$TEMPLATE" "$ROOT_DIR" "$AGENT_BINARY" "$TARGET"

chmod 644 "$TARGET"

# 3. Валидация
if ! plutil -lint "$TARGET" >/dev/null; then
  fail "plutil lint failed для $TARGET"
fi

# 4. Bootstrap
log "bootstrap gui/$UID_NUM $TARGET"
launchctl bootstrap "gui/$UID_NUM" "$TARGET" || fail "bootstrap failed"

# 5. Smoke test: ждём process в pgrep до 10 секунд
log "ждём процесс KrabEarAgent..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if pgrep -f "/Krab Ear.app/Contents/MacOS/KrabEarAgent" >/dev/null 2>&1; then
    log "✅ agent запущен через launchd"
    log "   status:  launchctl print gui/$UID_NUM/$LABEL"
    log "   stop:    launchctl bootout gui/$UID_NUM/$LABEL"
    log "   logs:    tail -f $ROOT_DIR/logs/krab-ear-agent.out.log"
    log ""
    log "ℹ️  Когда agent крашится — launchd respawn'ит за 10s. SingleInstanceGuard"
    log "   разрулит конфликт если ты дополнительно откроешь .app через Dock."
    exit 0
  fi
  sleep 1
done

fail "agent не появился за 10 сек. Смотри $ROOT_DIR/logs/krab-ear-agent.err.log"
