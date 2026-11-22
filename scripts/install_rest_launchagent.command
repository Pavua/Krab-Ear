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

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT_DIR/KrabEar/launchagents/ai.krab.ear.rest.plist.template"
TARGET="$HOME/Library/LaunchAgents/ai.krab.ear.rest.plist"
LABEL="ai.krab.ear.rest"
UID_NUM="$(id -u)"

log() { printf '[install-rest] %s\n' "$*"; }
fail() { printf '[install-rest] ❌ %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || fail "template not found: $TEMPLATE"
[ -d "$ROOT_DIR/.venv_krab_ear" ] || fail "venv missing: $ROOT_DIR/.venv_krab_ear (run Start Krab Ear.command once)"

# 🔴 Busy-гейт ДО любых изменений: перезагрузка REST-юнита рвёт идущие STT-запросы
# и (в in-process режиме) соседствует с записью. Занято/неизвестно → отказ.
if [ "$FORCE" -ne 1 ]; then
  "$ROOT_DIR/scripts/safe_backend_restart.command" --check-only >/dev/null 2>&1 \
    || fail "backend занят записью/встречей или состояние неизвестно — REST не тронут (--force обходит гейт)"
fi

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

# Успех bootstrap не доказывает, что сервер поднялся: ждём загруженный юнит с pid (до 15с).
STARTED=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -q 'pid = [0-9]'; then
    STARTED=1
    break
  fi
  sleep 1
done
[ "$STARTED" -eq 1 ] || fail "$LABEL загружен, но процесс не запустился за 15с (launchctl print gui/$UID_NUM/$LABEL, logs/krab-ear-rest.err.log)"

log "✅ ai.krab.ear.rest установлен и запущен."
log "Логи: $ROOT_DIR/logs/krab-ear-rest.{out,err}.log"
log "Статус: launchctl print gui/$UID_NUM/$LABEL"
