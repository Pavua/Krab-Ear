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

# S3/Р3 (busy-гейт, находка I-D): --wait N ждёт освобождения вместо отказа,
# --force — осознанный обход (источник правды по флагам/exit-кодам —
# scripts/safe_backend_restart.command).
WAIT_SEC=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) WAIT_SEC="${2:-60}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

log() { printf '[install] %s\n' "$*"; }
fail() { printf '[install] ❌ %s\n' "$*" >&2; exit 1; }

# S3-хвост (2026-08-04/05): фиксированный `sleep 1` после bootout не
# гарантирует, что launchd реально освободил label — на загруженной машине
# (живой пример того же дня: load average 294 после перезагрузки) bootout
# может занять больше секунды, и следующий bootstrap того же label рискует
# race'ом со старым, ещё не до конца выгруженным сервисом на том же
# socket path. Опрос вместо слепого ожидания — тот же принцип, что уже
# использует smoke-test ping ниже по скрипту (цикл до 15с, а не один sleep).
wait_for_bootout() {
  local label="$1"
  local timeout_sec="${2:-5}"
  local waited=0
  while launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; do
    if [ "$waited" -ge "$timeout_sec" ]; then
      log "⚠️ bootout не подтверждён за ${timeout_sec}с — продолжаю (launchd мог не успеть обновить состояние)"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}

# Дословная копия ipc_call()/busy_reason() из safe_backend_restart.command —
# единый источник правды по семантике занятости backend (S3/Р3). Дублирование
# осознанное: оба скрипта самодостаточны (без общего source'а), а
# test_install_backend_busy_gate_contract_S3.py извлекает их из ТЕКСТА этого
# файла и гоняет изолированно, без запуска установщика целиком.
ipc_call() {
  # Состояние выдаётся только после полного валидного ответа IPC. Тексты
  # диктовки/встречи не попадают ни в shell output, ни в аргументы процессов.
  python3 - "$1" <<'PY' 2>/dev/null
import json, os, socket, sys, time
method = sys.argv[1]
p = os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

try:
    deadline = time.monotonic() + 4.0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(4.0)
        connection.connect(p)
        connection.sendall((json.dumps({"id": "1", "method": method, "params": {}}) + "\n").encode())
        data = bytearray()
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(data) >= 1048576:
                raise TimeoutError("IPC reply limit")
            connection.settimeout(remaining)
            chunk = connection.recv(min(8192, 1048576 - len(data)))
            if not chunk:
                raise ValueError("incomplete IPC reply")
            data.extend(chunk)
    response = json.loads(data.split(b"\n", 1)[0], object_pairs_hook=unique_object)
    if not isinstance(response, dict) or response.get("id") != "1" or response.get("ok") is not True:
        raise ValueError("invalid RPC envelope")
    if response.get("error") is not None:
        raise ValueError("RPC error")
    result = response.get("result")
    if not isinstance(result, dict) or ("ok" in result and result["ok"] is not True):
        raise ValueError("invalid RPC result")
    if method == "ping":
        if result.get("status") != "ok":
            raise ValueError("backend not ready")
        print('{"ok": true}')
    else:
        field = {"get_recording_state": "is_recording", "get_meeting_live_state": "active"}[method]
        value = result.get(field)
        if type(value) is not bool or result.get("privacy_mode_active") is True:
            raise ValueError("activity unknown")
        print("busy" if value else "idle")
except Exception:
    if method != "ping":
        print("unknown")
PY
}

busy_reason() {
  # 0 + причина = занято/неизвестно (блокировать); 1 = оба IPC подтвердили idle.
  local rec meet
  rec=$(ipc_call get_recording_state)
  case "$rec" in
    busy) echo "recording"; return 0 ;;
    idle) ;;
    *) echo "unknown-recording"; return 0 ;;
  esac
  meet=$(ipc_call get_meeting_live_state)
  case "$meet" in
    busy) echo "meeting"; return 0 ;;
    idle) return 1 ;;
    *) echo "unknown-meeting"; return 0 ;;
  esac
}

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

# Busy-гейт перед переустановкой юнита (инцидент 2026-07-22: bootout под
# диктовкой безвозвратно теряет аудио — оно живёт только в памяти процесса).
# Отсутствие сокета не доказывает отсутствие владельца аудио. Первичная
# установка/recovery без IPC требуют существующего явного --force.
if [ "$FORCE" -eq 1 ]; then
  log "--force: busy-гейт пропущен"
else
  DEADLINE=$(( $(date +%s) + WAIT_SEC ))
  # 🔴 Вызывать busy_reason ТОЛЬКО внутри if/while: скрипт под `set -e`
  # (строка 16 выше), а busy_reason по контракту возвращает 1, когда backend
  # свободен. Голый `REASON=$(busy_reason)` отдельным statement'ом под set -e
  # молча завершил бы установку на КАЖДОМ свободном прогоне — гейт
  # инвертировался бы в вечный отказ (см. test_busy_reason_never_called_as_bare_assignment_under_set_e).
  while REASON=$(busy_reason); do
    if [ "$WAIT_SEC" -eq 0 ]; then
      fail "активная сессия ($REASON) или состояние не подтверждено — переустановка запрещена. Дождись окончания, запусти с --wait N или осознанно --force."
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      fail "сессия ($REASON) не закончилась за ${WAIT_SEC}с"
    fi
    log "идёт $REASON — жду… ($(( DEADLINE - $(date +%s) ))с осталось)"
    sleep 3
  done
fi

# 1. Bootout старой версии (если загружена) — ДО записи нового plist, чтобы
#    launchd не держал ссылку на старый путь/env.
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  log "bootout gui/$UID_NUM/$LABEL (была загружена старая версия)"
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>&1 || true
  wait_for_bootout "$LABEL" 5
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
