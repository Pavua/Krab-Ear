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
# с --wait). Мёртвый/неотвечающий сокет — рестарт разрешён (чинить и надо).
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
  # $1 = method; выводит сырой JSON-ответ или пустую строку при мёртвом сокете.
  python3 - "$1" <<'PY' 2>/dev/null
import json, os, socket, sys
method = sys.argv[1]
p = os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(4)
    s.connect(p)
    s.sendall((json.dumps({"id": "1", "method": method, "params": {}}) + "\n").encode())
    print(s.recv(8192).decode())
except Exception:
    pass
PY
}

busy_reason() {
  # Печатает причину занятости ("recording" / "meeting") или ничего.
  local rec meet
  rec=$(ipc_call get_recording_state)
  if [ -n "$rec" ] && printf '%s' "$rec" | grep -q '"is_recording": true'; then
    echo "recording"
    return 0
  fi
  meet=$(ipc_call get_meeting_live_state)
  # Активная meeting-сессия: state не idle/absent. Мягкий греп по "active".
  if [ -n "$meet" ] && printf '%s' "$meet" | grep -qE '"(state|status)": "(recording|active|running)"'; then
    echo "meeting"
    return 0
  fi
  return 1
}

if [ -S "$SOCK" ]; then
  DEADLINE=$(( $(date +%s) + WAIT_SEC ))
  while REASON=$(busy_reason); do
    if [ "$WAIT_SEC" -eq 0 ]; then
      echo "REFUSED: активная сессия ($REASON) — рестарт потерял бы аудио." >&2
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
else
  echo "[safe-restart] сокет отсутствует/мёртв — рестарт разрешён без проверки."
fi

echo "[safe-restart] kickstart $BACKEND_UNIT"
launchctl kickstart -k "$BACKEND_UNIT"
if [ "$WITH_REST" -eq 1 ]; then
  echo "[safe-restart] kickstart $REST_UNIT"
  launchctl kickstart -k "$REST_UNIT"
fi

# Ожидание готовности: ping до 60с (warmup тяжёлых моделей идёт дольше, но
# сокет поднимается раньше; нам важна IPC-доступность для GUI).
for _ in $(seq 1 30); do
  sleep 2
  RESP=$(ipc_call ping)
  if [ -n "$RESP" ] && printf '%s' "$RESP" | grep -q '"ok": true'; then
    NEW_PID=$(launchctl print "$BACKEND_UNIT" 2>/dev/null | grep -m1 'pid = ' | tr -dc '0-9')
    echo "[safe-restart] OK: backend жив (pid=${NEW_PID:-?}), IPC ping ok."
    exit 0
  fi
done
echo "FAIL: backend не ответил на ping за 60с после рестарта." >&2
exit 1
