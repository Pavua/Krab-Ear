#!/bin/bash
# Install/reinstall ai.krab.ear.backend LaunchAgent (Variant B supervisor).
#
# Что делает:
#   1. Читает template `KrabEar/launchagents/ai.krab.ear.backend.plist.template`
#   2. Подставляет __HOME__, __PROJECT_ROOT__, __HF_TOKEN__ из окружения/.secrets
#   3. bootout старой версии (если есть) → записывает plist → bootstrap
#   4. Smoke test: ждёт IPC ping на сокете до 15 секунд
#
# Зачем вообще launchd (Variant B) вместо Swift BackendSupervisor:
#   Swift hand-rolled supervisor респавнит backend ТОЛЬКО при IPC call failure,
#   много call-сайтов обходят `callWithRecovery()`. GUI «висел» пока backend
#   мёртв, хоть и plist живёт. launchd KeepAlive=true респавнит за
#   ThrottleInterval (5 сек) независимо от IPC traffic.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT_DIR/KrabEar/launchagents/ai.krab.ear.backend.plist.template"
TARGET="$HOME/Library/LaunchAgents/ai.krab.ear.backend.plist"
LABEL="ai.krab.ear.backend"
SOCKET="$HOME/Library/Application Support/KrabEar/krabear.sock"
UID_NUM="$(id -u)"

log() { printf '[install] %s\n' "$*"; }
fail() { printf '[install] ❌ %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || fail "template not found: $TEMPLATE"
[ -d "$ROOT_DIR/.venv_krab_ear" ] || fail "venv missing: $ROOT_DIR/.venv_krab_ear (run Start Krab Ear.command once)"
mkdir -p "$ROOT_DIR/logs"
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/Library/Application Support/KrabEar"

# HF_TOKEN resolution order:
#   1. env var HF_TOKEN (если пользователь выставил вручную)
#   2. ~/Library/Application Support/KrabEar/.secrets (KRAB_EAR_HF_TOKEN=...)
#   3. интерактивный prompt (fallback)
# Сам .secrets файл НЕ коммитится (gitignore), живёт вне репо.
SECRETS_FILE="$HOME/Library/Application Support/KrabEar/.secrets"
if [ -z "${HF_TOKEN:-}" ] && [ -f "$SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  HF_TOKEN_FROM_SECRETS="$(grep -E '^KRAB_EAR_HF_TOKEN=' "$SECRETS_FILE" | head -1 | cut -d= -f2- || true)"
  [ -n "$HF_TOKEN_FROM_SECRETS" ] && HF_TOKEN="$HF_TOKEN_FROM_SECRETS"
fi
# Токен ОПЦИОНАЛЕН: нужен только для pyannote-диаризации говорящих. Пустой ввод
# (или KRAB_EAR_SKIP_HF_TOKEN=1 для неинтерактивных вызовов) = пропустить —
# HF-ключи тогда целиком вырезаются из plist. Пустую строку в env оставлять
# НЕЛЬЗЯ: битый/пустой HF_TOKEN ломает даже анонимные загрузки моделей (401).
if [ -z "${HF_TOKEN:-}" ] && [ "${KRAB_EAR_SKIP_HF_TOKEN:-0}" != "1" ]; then
  printf '[install] HF-токен (Hugging Face, для диаризации говорящих — опционально).\n'
  printf '[install] Enter = пропустить (диаризация отключится, остальное работает): '
  read -r HF_TOKEN
fi
if [ -z "${HF_TOKEN:-}" ]; then
  log "HF-токен пропущен — диаризация говорящих отключена"
  HF_TOKEN=""
fi

# 1. Bootout старой версии (если загружена) — ДО записи нового plist, чтобы
#    launchd не держал ссылку на старый путь/env.
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  log "bootout gui/$UID_NUM/$LABEL (была загружена старая версия)"
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>&1 || true
  sleep 1
fi

# 2. Substitution template → target. sed с | разделителем потому что пути и
#    токен могут содержать `/`. __HF_TOKEN__ подставляем последним чтобы
#    возможные спецсимволы в токене не зацепили предыдущие replace'ы.
log "render template → $TARGET"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__PROJECT_ROOT__|$ROOT_DIR|g" \
    "$TEMPLATE" > "$TARGET.tmp"
# Для HF_TOKEN используем python чтобы не зависеть от sed escaping.
# Пустой токен = диаризация пропущена: удаляем HF-ключи из plist целиком.
python3 -c "
import sys, pathlib, re
p = pathlib.Path('$TARGET.tmp')
text = p.read_text()
token = sys.argv[1]
if token:
    text = text.replace('__HF_TOKEN__', token)
else:
    text = re.sub(
        r'\s*<key>(?:KRAB_EAR_)?HF_TOKEN</key>\s*<string>__HF_TOKEN__</string>',
        '', text)
p.write_text(text)
" "$HF_TOKEN"
mv "$TARGET.tmp" "$TARGET"
chmod 600 "$TARGET"  # plist содержит secret, ограничиваем доступ

# 3. Валидация через plutil — убедиться что XML валиден до bootstrap
if ! plutil -lint "$TARGET" >/dev/null; then
  fail "plutil lint failed для $TARGET (проверь template)"
fi

# 4. Bootstrap в gui domain текущего пользователя
log "bootstrap gui/$UID_NUM $TARGET"
launchctl bootstrap "gui/$UID_NUM" "$TARGET" || fail "bootstrap failed"

# 5. Smoke test: ждём IPC ping до 15 секунд (backend стартует ~5-8с на загрузку
#    Whisper + pyannote моделей при первом запуске).
log "ждём IPC ping на $SOCKET ..."
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if [ -S "$SOCKET" ]; then
    PING_RESULT="$(python3 -c "
import socket, json, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect('$SOCKET')
    s.sendall((json.dumps({'id':'install-smoke','method':'ping','params':{}})+'\n').encode())
    data = s.recv(4096).decode()
    print(data.strip())
except Exception as e:
    print(f'ERR: {e}')
    sys.exit(1)
finally:
    s.close()
" 2>&1)"
    if echo "$PING_RESULT" | grep -q '"ok":\s*true'; then
      log "✅ backend ответил: $PING_RESULT"
      log "✅ Variant B установлен. Label: $LABEL"
      log "   status:  launchctl print gui/$UID_NUM/$LABEL"
      log "   stop:    launchctl bootout gui/$UID_NUM/$LABEL"
      log "   logs:    tail -f $ROOT_DIR/logs/krab-ear-backend.out.log"
      exit 0
    fi
  fi
  sleep 1
done

fail "backend не ответил за 15 сек. Смотри $ROOT_DIR/logs/krab-ear-backend.err.log"
