#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# safe_backend_restart.command — рестарт IPC-бэкенда БЕЗ убийства живой диктовки.
#
# Инцидент 2026-07-22 02:05 CEST: деплой сделал `launchctl kickstart -k` в
# момент, когда владелец диктовал через GUI → сокет умер под записью → две
# диктовки потеряны с «Backend не ответил за таймаут». Аудио живёт в памяти
# backend-процесса — kickstart под активной записью теряет его безвозвратно.
#
# Скрипт спрашивает get_recording_state / get_meeting_live_state через IPC и
# отказывается рестартовать, пока идёт запись или живая встреча (можно ждать
# с --wait). Отсутствующий/неотвечающий сокет — UNKNOWN, не разрешение
# убивать процесс. Восстановление неизвестного endpoint требует отдельного
# осознанного действия владельца; автоматический recovery здесь запрещён.
#
# ИСПОЛЬЗОВАНИЕ:
#   scripts/safe_backend_restart.command            # backend, отказ при записи
#   scripts/safe_backend_restart.command --wait 120 # ждать окончания до 120с
#   scripts/safe_backend_restart.command --with-rest# + рестарт REST-юнита
#
# ⚠️ ВНЕШНИЙ КОНТРАКТ: скрипт вызывается лаунчерами Voice Gateway (их PR #113,
# 2026-07-22) — имя файла, флаги (--wait/--with-rest) и exit-коды (0 ok /
# 1 refused-or-fail / 2 usage) менять только по координации с VG-сессией.
# ---------------------------------------------------------------------------
set -uo pipefail

SOCK="$HOME/Library/Application Support/KrabEar/krabear.sock"
BACKEND_UNIT="gui/$(id -u)/ai.krab.ear.backend"
REST_UNIT="gui/$(id -u)/ai.krab.ear.rest"

WAIT_SEC=0
WITH_REST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) WAIT_SEC="${2:-60}"; shift 2 ;;
    --with-rest) WITH_REST=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

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

DEADLINE=$(( $(date +%s) + WAIT_SEC ))
while REASON=$(busy_reason); do
  if [ "$WAIT_SEC" -eq 0 ]; then
    echo "REFUSED: активная сессия ($REASON) или состояние не подтверждено — рестарт запрещён." >&2
    echo "Дождись окончания или запусти с --wait N." >&2
    exit 1
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "REFUSED: сессия ($REASON) не закончилась за ${WAIT_SEC}с." >&2
    exit 1
  fi
  echo "[safe-restart] идёт $REASON — жду… ($(( DEADLINE - $(date +%s) ))с осталось)"
  sleep 3
done

echo "[safe-restart] kickstart $BACKEND_UNIT"
launchctl kickstart -k "$BACKEND_UNIT"

# Ожидание готовности: ping до 60с (warmup тяжёлых моделей идёт дольше, но
# сокет поднимается раньше; нам важна IPC-доступность для GUI).
# REST поднимаем ТОЛЬКО после ping: параллельный kickstart даёт новому REST
# закэшировать bridge-токен до того, как новый backend допишет файл → 401
# на /internal/event (SSE/тосты молчат).
for _ in $(seq 1 30); do
  sleep 2
  RESP=$(ipc_call ping)
  if [ -n "$RESP" ] && printf '%s' "$RESP" | grep -q '"ok": true'; then
    NEW_PID=$(launchctl print "$BACKEND_UNIT" 2>/dev/null | grep -m1 'pid = ' | tr -dc '0-9')
    echo "[safe-restart] OK: backend жив (pid=${NEW_PID:-?}), IPC ping ok."
    if [ "$WITH_REST" -eq 1 ]; then
      echo "[safe-restart] kickstart $REST_UNIT (после IPC ping)"
      launchctl kickstart -k "$REST_UNIT"
    fi
    exit 0
  fi
done
echo "FAIL: backend не ответил на ping за 60с после рестарта." >&2
exit 1
