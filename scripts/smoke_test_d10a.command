#!/bin/bash
# D.10a smoke test — end-to-end LLM rewriter verification.
#
# Что проверяет:
#   1. Backend инициализировал LLM rewriter (log grep)
#   2. llm_status IPC возвращает reachable=true, circuit=closed
#   3. Включает runtime toggle
#   4. Пингует backend чтобы убедиться что работает
#
# НЕ проверяет настоящую диктовку — это делается вручную через GUI.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SOCKET="$HOME/Library/Application Support/KrabEar/krabear.sock"
LOG_FILE="$ROOT_DIR/logs/krab-ear-backend.out.log"

log() { printf '[smoke] %s\n' "$*"; }
fail() { printf '[smoke] ❌ %s\n' "$*" >&2; exit 1; }

ipc_call() {
  # $1 = метод, $2 = JSON params (по умолчанию {}). Читает ответ ДО перевода строки:
  # одиночный recv(8192) отдавал часть ответа (или обрывал UTF-8 символ посередине) и
  # давал ложный провал на корректном backend. Дедлайн 15с и потолок 1 МиБ.
  local params="${2:-}"
  [ -n "$params" ] || params='{}'
  python3 - "$SOCKET" "$1" "$params" <<'PYEND'
import json, os, socket, sys, time
path, method, params = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
deadline = time.monotonic() + float(os.environ.get("KE_IPC_DEADLINE", "15"))
CAP = 1 << 20
buf = b""
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(path)
    s.sendall((json.dumps({"id": method, "method": method, "params": params}) + "\n").encode())
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ответ не завершён за дедлайн")
        s.settimeout(min(5.0, remaining))  # капельная отправка не продлевает общий дедлайн
        chunk = s.recv(65536)
        if not chunk:
            raise ConnectionError("соединение закрыто до конца ответа")
        buf += chunk
        if len(buf) > CAP:  # потолок — ПОСЛЕ добавления куска, до приёма перевода строки
            raise OverflowError("ответ больше 1 МиБ")
    s.close()
    print(buf.split(b"\n", 1)[0].decode("utf-8"))
except Exception as exc:
    print(f"ipc_call {method}: {exc}", file=sys.stderr)
    sys.exit(1)
PYEND
}

# 1. Socket exists
[ -S "$SOCKET" ] || fail "socket not found: $SOCKET (backend not running?)"

# 2. Backend log contains LLM initialization message
if ! grep -q "LLM rewriter инициализирован" "$LOG_FILE" 2>/dev/null; then
  log "⚠️  Не найдено 'LLM rewriter инициализирован' в $LOG_FILE"
  log "    Возможные причины: KRAB_EAR_LLM_ENABLED=false или старый backend"
  log "    Попробуй: launchctl kickstart -k gui/$(id -u)/ai.krab.ear.backend"
  fail "LLM rewriter не инициализирован"
fi
log "✅ LLM rewriter инициализирован (найдено в логе)"

# 3. Call llm_status IPC
log "запрос llm_status..."
STATUS_JSON="$(ipc_call llm_status)" || fail "llm_status IPC не ответил"

if ! echo "$STATUS_JSON" | grep -q '"ok":\s*true'; then
  fail "llm_status IPC failed: $STATUS_JSON"
fi

log "llm_status response: $STATUS_JSON"

MODEL="$(echo "$STATUS_JSON" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('model','<none>'))")"
CIRCUIT="$(echo "$STATUS_JSON" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('circuit_state','<none>'))")"
REACHABLE="$(echo "$STATUS_JSON" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('reachable',False))")"

log "  model=$MODEL"
log "  circuit_state=$CIRCUIT"
log "  reachable=$REACHABLE"

[ "$CIRCUIT" = "closed" ] || fail "circuit_state not closed: $CIRCUIT"
[ "$REACHABLE" = "True" ] || fail "reachable not True: $REACHABLE"

# 4. Enable runtime toggle
log "включение runtime toggle (llm_rewrite_enabled=true)..."
TOGGLE_JSON="$(ipc_call set_settings '{"settings":{"llm_rewrite_enabled":true}}')" || fail "set_settings IPC не ответил"

if ! echo "$TOGGLE_JSON" | grep -q '"ok":\s*true'; then
  fail "set_settings toggle failed: $TOGGLE_JSON"
fi
log "✅ runtime toggle включён"

# 5. Re-check llm_status that enabled is now true
STATUS_JSON2="$(ipc_call llm_status)" || fail "повторный llm_status IPC не ответил"

ENABLED="$(echo "$STATUS_JSON2" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('enabled',False))")"
RUNTIME="$(echo "$STATUS_JSON2" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('runtime_enabled',False))")"

log "  enabled=$ENABLED"
log "  runtime_enabled=$RUNTIME"

[ "$ENABLED" = "True" ] || fail "overall enabled not True after toggle: $ENABLED"

log "✅ D.10a smoke test пройден"
log ""
log "Следующий шаг — manual test: диктуй через Right Option и проверь что текст"
log "попал с правильной пунктуацией. Смотри $LOG_FILE на 'LLM rewrite:' строки."
