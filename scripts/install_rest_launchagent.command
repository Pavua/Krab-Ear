#!/bin/bash
# Install/reinstall ai.krab.ear.rest LaunchAgent (REST server, port 5005).
#
# Что делает:
#   1. Читает template `KrabEar/launchagents/ai.krab.ear.rest.plist.template`
#   2. Подставляет __HOME__, __PROJECT_ROOT__, __HF_TOKEN__ из окружения/.secrets
#   3. bootout старой версии (если есть) → записывает plist → bootstrap
#
# Wave 70: шаблон содержит explicit PATH=/opt/homebrew/bin:... чтобы
# ffmpeg был доступен в launchd-окружении для gigaam subprocess.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT_DIR/KrabEar/launchagents/ai.krab.ear.rest.plist.template"
TARGET="$HOME/Library/LaunchAgents/ai.krab.ear.rest.plist"
LABEL="ai.krab.ear.rest"
UID_NUM="$(id -u)"

log() { printf '[install-rest] %s\n' "$*"; }
fail() { printf '[install-rest] ❌ %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || fail "template not found: $TEMPLATE"
[ -d "$ROOT_DIR/.venv_krab_ear" ] || fail "venv missing: $ROOT_DIR/.venv_krab_ear (run Start Krab Ear.command once)"
mkdir -p "$ROOT_DIR/logs"
mkdir -p "$HOME/Library/LaunchAgents"

# HF_TOKEN resolution (same as install_backend_launchagent.command)
SECRETS_FILE="$HOME/Library/Application Support/KrabEar/.secrets"
if [ -z "${HF_TOKEN:-}" ] && [ -f "$SECRETS_FILE" ]; then
  HF_TOKEN_FROM_SECRETS="$(grep -E '^KRAB_EAR_HF_TOKEN=' "$SECRETS_FILE" | head -1 | cut -d= -f2- || true)"
  [ -n "$HF_TOKEN_FROM_SECRETS" ] && HF_TOKEN="$HF_TOKEN_FROM_SECRETS"
fi
if [ -z "${HF_TOKEN:-}" ]; then
  printf '[install-rest] HF_TOKEN не найден. Введи token (Hugging Face): '
  read -r HF_TOKEN
  [ -n "$HF_TOKEN" ] || fail "HF_TOKEN пустой, abort"
fi

# 1. Bootout старой версии
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  log "bootout gui/$UID_NUM/$LABEL"
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>&1 || true
  sleep 1
fi

# 2. Render template → target
log "render template → $TARGET"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__PROJECT_ROOT__|$ROOT_DIR|g" \
    "$TEMPLATE" > "$TARGET.tmp"
python3 -c "
import sys, pathlib
p = pathlib.Path('$TARGET.tmp')
p.write_text(p.read_text().replace('__HF_TOKEN__', sys.argv[1]))
" "$HF_TOKEN"
mv "$TARGET.tmp" "$TARGET"
chmod 600 "$TARGET"

# 3. Validate
if ! plutil -lint "$TARGET" >/dev/null; then
  fail "plutil lint failed для $TARGET"
fi
log "plutil lint OK"

# 4. Bootstrap
log "bootstrap gui/$UID_NUM/$LABEL"
launchctl bootstrap "gui/$UID_NUM" "$TARGET"

log "✅ ai.krab.ear.rest установлен и запущен."
log "Логи: $ROOT_DIR/logs/krab-ear-rest.{out,err}.log"
log "Статус: launchctl print gui/$UID_NUM/$LABEL"
