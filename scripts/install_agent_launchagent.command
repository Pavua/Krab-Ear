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

# 1. Render template во ВРЕМЕННЫЙ файл в каталоге установки (тот же ФС → атомарный mv).
# Python — для безопасного replace (пути могут содержать пробелы, например "Krab Ear.app").
STAGED="$(mktemp "$(dirname "$TARGET")/.ai.krab.ear.agent.XXXXXX")"
trap 'rm -f "$STAGED"' EXIT
log "render template → $STAGED"
python3 -c "
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()
src = src.replace('__PROJECT_ROOT__', sys.argv[2])
src = src.replace('__AGENT_BINARY__', sys.argv[3])
pathlib.Path(sys.argv[4]).write_text(src)
" "$TEMPLATE" "$ROOT_DIR" "$AGENT_BINARY" "$STAGED"
chmod 644 "$STAGED"

# 2. Валидация ДО любых изменений: невалидный plist не должен ни выгрузить рабочий юнит,
# ни затереть установленный файл.
if ! plutil -lint "$STAGED" >/dev/null; then
  fail "plutil lint failed для нового plist — установленный юнит не тронут"
fi

# 3. Bootout старой версии (если загружена), затем атомарная подмена файла.
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  log "bootout gui/$UID_NUM/$LABEL (была загружена старая версия)"
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>&1 || true
  sleep 1
fi
mv -f "$STAGED" "$TARGET"
STAGED=""

# 4. Bootstrap
log "bootstrap gui/$UID_NUM $TARGET"
launchctl bootstrap "gui/$UID_NUM" "$TARGET" || fail "bootstrap failed"

# 5. Smoke test: ждём, что ИМЕННО этот launchd-юнит получил pid (до 10 секунд).
# pgrep по пути бинаря принимал за запуск любой подходящий процесс (например, открытый
# вручную через Dock), хотя юнит мог не стартовать.
log "ждём запуск launchd-юнита $LABEL..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -q 'pid = [0-9]'; then
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

fail "юнит $LABEL не получил pid за 10 сек. Смотри $ROOT_DIR/logs/krab-ear-agent.err.log"
