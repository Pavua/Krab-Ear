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
  read -rs HF_TOKEN
  printf '\n'
  [ -n "$HF_TOKEN" ] || fail "HF_TOKEN пустой, abort"
fi

# Подготавливаем и проверяем приватный plist до остановки сервиса.
log "render template → $TARGET"
STAGED_PLIST=""
trap '[ -z "$STAGED_PLIST" ] || rm -f -- "$STAGED_PLIST"' EXIT
STAGED_PLIST="$(printf '%s' "$HF_TOKEN" |
  python3 "$ROOT_DIR/scripts/render_launchagent_plist.py" "$TEMPLATE" "$TARGET" "$HOME" "$ROOT_DIR")" ||
  fail "не удалось подготовить plist"
if ! plutil -lint "$STAGED_PLIST" >/dev/null; then
  fail "plutil lint failed для нового plist"
fi

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  log "bootout gui/$UID_NUM/$LABEL"
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>&1 || true
  sleep 1
fi

# Подмена в том же каталоге сохраняет права временного файла.
mv -f -- "$STAGED_PLIST" "$TARGET"
STAGED_PLIST=""

# 4. Bootstrap
log "bootstrap gui/$UID_NUM/$LABEL"
launchctl bootstrap "gui/$UID_NUM" "$TARGET"

log "✅ ai.krab.ear.rest установлен и запущен."
log "Логи: $ROOT_DIR/logs/krab-ear-rest.{out,err}.log"
log "Статус: launchctl print gui/$UID_NUM/$LABEL"
