#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# migrate_to_inprocess_rest.command — переход с отдельного launchd-юнита
# ai.krab.ear.rest на режим «REST внутри backend-процесса» (спека
# docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md, §4).
#
# Скрипт делает ОДНО: выгружает (`launchctl bootout`) легаси REST-юнит.
# Он НЕ включает рубильник rest_in_process_enabled и НЕ рестартует backend —
# это отдельный шаг канарейки (координаторская задача 12 плана волны).
#
# 🔴 plist легаси-юнита НИКОГДА не удаляется этим скриптом. Файл на диске —
# единственный механизм отката на всё время двухнедельной канарейки; его
# удаление — отдельный шаг ПОСЛЕ канарейки, вне этой волны.
#
# 🔴 Busy-гейт — первым делом, до любого launchctl-вызова в любом направлении.
# Инцидент 2026-07-22: kickstart -k backend-юнита во время диктовки убил
# аудио, живущее в памяти процесса («Backend не ответил за таймаут», две
# записи потеряны). Здесь риск тот же класс — переустановка REST-поверхности
# ровно во время активной записи/встречи. Проверяем get_recording_state и
# get_meeting_live_state через IPC по образцу safe_backend_restart.command
# (busy_reason()) и отказываемся, пока не поддержан --wait.
#
# ИСПОЛЬЗОВАНИЕ:
#   scripts/migrate_to_inprocess_rest.command             # выгрузить легаси REST
#   scripts/migrate_to_inprocess_rest.command --wait 120   # ждать окончания записи/встречи до 120с
#   scripts/migrate_to_inprocess_rest.command --rollback   # вернуть легаси REST (парный откат)
#   scripts/migrate_to_inprocess_rest.command --force      # осознанно: backend мёртв/завис, гейт не применять
#
# 🔴 Гейт fail-closed: пустой/невалидный ответ IPC и отсутствующий сокет — это
# «состояние неизвестно», а НЕ «записи нет» (тот же контракт, что у
# safe_backend_restart.command). Занято или неизвестно → отказ (код 1).
#
# Bash 3.2 / BSD-утилиты (macOS): без mapfile/readarray/declare -A; pgrep=ERE;
# GNU timeout отсутствует — не используются.
# ---------------------------------------------------------------------------
set -uo pipefail

SOCK="$HOME/Library/Application Support/KrabEar/krabear.sock"
REST_PLIST="$HOME/Library/LaunchAgents/ai.krab.ear.rest.plist"
REST_LABEL="ai.krab.ear.rest"
UID_NUM="$(id -u)"
REST_UNIT="gui/$UID_NUM/$REST_LABEL"

WAIT_SEC=0
ROLLBACK=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) WAIT_SEC="${2:-60}"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --force) FORCE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ipc_call() {
  # $1 = метод; печатает сырой JSON-ответ бэкенда или пустую строку при
  # мёртвом/отсутствующем сокете (не бросает — вызывающий сам решает, что
  # делать с пустотой).
  python3 - "$1" <<'PY' 2>/dev/null
import json, os, socket, sys
method = sys.argv[1]
p = os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(4)
    s.connect(p)
    s.sendall((json.dumps({"id": "1", "method": method, "params": {}}) + "\n").encode())
    print(s.recv(65536).decode())
except Exception:
    pass
PY
}

classify_state() {
  # $1 = recording|meeting, $2 = сырой ответ IPC. Печатает busy | idle | unknown.
  # СТРОГИЙ разбор JSON: объект, ok строго true, result — объект, поле — настоящий bool.
  # Подстрока "is_recording": false внутри не-JSON или в ответе с ok:false — это unknown.
  python3 - "$1" "$2" <<'PYEND' 2>/dev/null
import json, sys
kind, raw = sys.argv[1], sys.argv[2]
try:
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        raise ValueError("envelope")
    result = doc.get("result")
    if not isinstance(result, dict):
        raise ValueError("result")
    value = result.get("is_recording" if kind == "recording" else "active")
    if not isinstance(value, bool):
        raise ValueError("field")
    print("busy" if value else "idle")
except Exception:
    print("unknown")
PYEND
}

busy_reason() {
  # 0 + причина = занято ИЛИ состояние не подтверждено (блокировать);
  # 1 = оба IPC дали валидный ответ с явным false (get_recording_state / get_meeting_live_state).
  local rec meet
  rec=$(ipc_call get_recording_state)
  case "$(classify_state recording "$rec")" in
    busy) echo "recording"; return 0 ;;
    idle) ;;
    *) echo "unknown-recording"; return 0 ;;
  esac
  meet=$(ipc_call get_meeting_live_state)
  case "$(classify_state meeting "$meet")" in
    busy) echo "meeting"; return 0 ;;
    idle) return 1 ;;
    *) echo "unknown-meeting"; return 0 ;;
  esac
}

rest_inprocess_running() {
  # $1 = сырой JSON get_diagnostics. Печатает "true" / "false" / "unknown".
  # "unknown" — любой сбой разбора ИЛИ пустой ввод (мёртвый сокет, кривой
  # JSON, отсутствие секции) — намеренно НЕ трактуется как "false": для
  # --rollback это должно читаться как отказ (fail-closed), иначе владелец
  # рискует поднять легаси-юнит поверх занятого порта.
  # JSON передаётся через argv, НЕ через stdin — `python3 - <<PY` сам читает
  # heredoc как ИСХОДНЫЙ КОД скрипта из stdin, повторный sys.stdin.read()
  # внутри увидел бы пустой поток (тот же паттерн, что ipc_call()).
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    doc = json.loads(sys.argv[1])
    running = doc.get("result", {}).get("rest_in_process", {}).get("running")
    if running is True:
        print("true")
    elif running is False:
        print("false")
    else:
        print("unknown")
except Exception:
    print("unknown")
PY
}

print_diagnostics_summary() {
  # $1 = сырой JSON get_diagnostics. Печатает каталог данных и состояние
  # in-process REST — верификация после операции (задача 11, пункт 4).
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    doc = json.loads(sys.argv[1])
    result = doc.get("result", {})
    data_dir = result.get("history", {}).get("data_dir", "?")
    rip = result.get("rest_in_process", {})
    print("  data_dir: %s" % data_dir)
    print(
        "  rest_in_process: enabled=%s running=%s port=%s error=%s"
        % (rip.get("enabled"), rip.get("running"), rip.get("port"), rip.get("error"))
    )
except Exception as exc:
    print("  (не удалось разобрать get_diagnostics: %s)" % exc)
PY
}

# ── Busy-гейт: первым делом, до любого launchctl-вызова ──────────────────────
# Применяется ВСЕГДА (в т.ч. при отсутствующем сокете), кроме явного --force.
if [ "$FORCE" -eq 1 ]; then
  echo "[migrate-rest] --force: busy-гейт пропущен по явной просьбе владельца."
else
  DEADLINE=$(( $(date +%s) + WAIT_SEC ))
  while REASON=$(busy_reason); do
    if [ "$WAIT_SEC" -eq 0 ]; then
      echo "REFUSED: активная сессия ($REASON) или состояние не подтверждено — переустановка REST-юнита рискует потерять аудио." >&2
      echo "Дождись окончания, запусти с --wait N, либо --force если backend заведомо мёртв." >&2
      exit 1
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "REFUSED: сессия ($REASON) не закончилась/не подтверждена за ${WAIT_SEC}с." >&2
      exit 1
    fi
    echo "[migrate-rest] $REASON — жду… ($(( DEADLINE - $(date +%s) ))с осталось)"
    sleep 3
  done
fi

# ── Ветка --rollback: вернуть легаси REST-юнит ────────────────────────────────
if [ "$ROLLBACK" -eq 1 ]; then
  echo "[migrate-rest] --rollback: проверяю get_diagnostics.rest_in_process.running…"
  DIAG=$(ipc_call get_diagnostics)
  RUNNING=$(rest_inprocess_running "$DIAG")
  if [ "$RUNNING" != "false" ]; then
    echo "REFUSED: сначала выключи режим настройкой и перезапусти backend, затем повтори --rollback." >&2
    echo "  (rest_in_process.running=$RUNNING — легаси REST на порту 5005 уйдёт в crash-loop EADDRINUSE, KeepAlive=true)" >&2
    exit 1
  fi

  [ -f "$REST_PLIST" ] || {
    echo "FAIL: plist не найден: $REST_PLIST (удалён вне процедуры волны — откат невозможен без него)." >&2
    exit 1
  }

  echo "[migrate-rest] bootstrap $REST_UNIT из $REST_PLIST"
  launchctl bootstrap "gui/$UID_NUM" "$REST_PLIST" 2>&1
  sleep 1

  LOADED=0
  i=1
  while [ "$i" -le 5 ]; do
    if launchctl print "$REST_UNIT" >/dev/null 2>&1; then
      LOADED=1
      break
    fi
    sleep 1
    i=$((i + 1))
  done
  if [ "$LOADED" -ne 1 ]; then
    echo "FAIL: $REST_LABEL не поднялся после bootstrap." >&2
    exit 1
  fi
  echo "[migrate-rest] OK: $REST_LABEL восстановлен (откат выполнен)."

  echo "[migrate-rest] Верификация:"
  print_diagnostics_summary "$(ipc_call get_diagnostics)"
  exit 0
fi

# ── Основная ветка: выгрузить легаси REST-юнит, plist оставить на диске ──────
if launchctl print "$REST_UNIT" >/dev/null 2>&1; then
  echo "[migrate-rest] bootout $REST_UNIT (plist остаётся на диске — механизм отката канарейки)"
  launchctl bootout "$REST_UNIT" 2>&1
else
  echo "[migrate-rest] $REST_LABEL уже не загружен — bootout не требуется."
fi
sleep 1

UNLOADED=0
i=1
while [ "$i" -le 5 ]; do
  if ! launchctl print "$REST_UNIT" >/dev/null 2>&1; then
    UNLOADED=1
    break
  fi
  sleep 1
  i=$((i + 1))
done
if [ "$UNLOADED" -ne 1 ]; then
  echo "FAIL: $REST_LABEL всё ещё загружен после bootout." >&2
  exit 1
fi

echo "[migrate-rest] OK: $REST_LABEL выгружен."
echo "[migrate-rest] plist сохранён на диске (механизм отката): $REST_PLIST"
echo ""
echo "Следующие шаги (координаторская задача 12, не этот скрипт):"
echo "  1. Включить режим: set_settings {rest_in_process_enabled: true}"
echo "  2. Рестарт backend: scripts/safe_backend_restart.command"
echo "  3. Откат режима: set_settings {rest_in_process_enabled: false} + рестарт backend"
echo "  4. Откат юнита: scripts/migrate_to_inprocess_rest.command --rollback"
echo ""
echo "[migrate-rest] Верификация:"
print_diagnostics_summary "$(ipc_call get_diagnostics)"
exit 0
