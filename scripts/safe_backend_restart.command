#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# safe_backend_restart.command — idle-проверка; рестарт без fence запрещён.
#
# Инцидент 2026-07-22 02:05 CEST: деплой сделал `launchctl kickstart -k` в
# момент, когда владелец диктовал через GUI → сокет умер под записью → две
# диктовки потеряны с «Backend не ответил за таймаут». Аудио живёт в памяти
# backend-процесса — kickstart под активной записью теряет его безвозвратно.
#
# Скрипт спрашивает get_recording_state / get_meeting_live_state через IPC.
# Idle-снимок не закрывает доступ новым запросам/записи после проверки и не
# подтверждает drain. Поэтому обычный рестарт сейчас всегда отказывает;
# --check-only сообщает только снимок. Восстановление неизвестного endpoint
# и безопасный cutover требуют отдельно квалифицированного механизма.
#
# ИСПОЛЬЗОВАНИЕ:
#   scripts/safe_backend_restart.command            # отказ: нет admission fence
#   scripts/safe_backend_restart.command --wait 120 # ждать окончания до 120с
#   scripts/safe_backend_restart.command --with-rest# тот же безопасный отказ
#   scripts/safe_backend_restart.command --check-only # только idle-проба, без рестарта
#
# ⚠️ ВНЕШНИЙ КОНТРАКТ: скрипт вызывается лаунчерами Voice Gateway (их PR #113,
# 2026-07-22) — имя файла, флаги (--wait/--with-rest) и exit-коды (0 ok /
# 1 refused-or-fail / 2 usage) менять только по координации с VG-сессией.
# ---------------------------------------------------------------------------
set -uo pipefail

WAIT_SEC=0
CHECK_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --wait)
      if [ "$#" -lt 2 ]; then
        echo "invalid --wait: expected nonnegative integer seconds" >&2
        exit 2
      fi
      WAIT_VALUE="$2"
      case "$WAIT_VALUE" in
        ''|*[!0-9]*) echo "invalid --wait: expected nonnegative integer seconds" >&2; exit 2 ;;
      esac
      if [ "${#WAIT_VALUE}" -gt 10 ]; then
        echo "invalid --wait: value too large" >&2
        exit 2
      fi
      WAIT_SEC=$((10#$WAIT_VALUE))
      if [ "$WAIT_SEC" -gt 2147483647 ]; then
        echo "invalid --wait: value too large" >&2
        exit 2
      fi
      shift 2
      ;;
    --with-rest) shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
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
        if method == "get_recording_state" and value is False:
            # Capture уже остановлен, но STT/history tail может ещё владеть G1.
            count = result.get("finalizing_count")
            finalizing = result.get("is_finalizing")
            if (type(count) is not int or count < 0
                    or type(finalizing) is not bool
                    or finalizing != (count > 0)):
                raise ValueError("finalization unknown")
            active_fields = ("owner", "generation_token", "start_request_id")
            if any(key not in result for key in active_fields):
                raise ValueError("generation unknown")
            value = finalizing or any(result[key] is not None for key in active_fields)
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

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "[safe-restart] IDLE: запись и встреча не активны; runtime не изменён."
  exit 0
fi

echo "REFUSED: idle-снимок не подтверждает admission fence и drain; рестарт запрещён." >&2
exit 1
