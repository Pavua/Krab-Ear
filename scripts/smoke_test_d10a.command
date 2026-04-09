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
STATUS_JSON="$(python3 -c "
import socket, json, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('$SOCKET')
s.sendall((json.dumps({'id':'smoke','method':'llm_status','params':{}})+'\n').encode())
data = s.recv(8192).decode()
print(data.strip())
s.close()
" 2>&1)"

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
TOGGLE_JSON="$(python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('$SOCKET')
s.sendall((json.dumps({
    'id':'toggle',
    'method':'set_settings',
    'params':{'settings':{'llm_rewrite_enabled':True}}
})+'\n').encode())
data = s.recv(8192).decode()
print(data.strip())
s.close()
")"

if ! echo "$TOGGLE_JSON" | grep -q '"ok":\s*true'; then
  fail "set_settings toggle failed: $TOGGLE_JSON"
fi
log "✅ runtime toggle включён"

# 5. Re-check llm_status that enabled is now true
STATUS_JSON2="$(python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('$SOCKET')
s.sendall((json.dumps({'id':'smoke2','method':'llm_status','params':{}})+'\n').encode())
data = s.recv(8192).decode()
print(data.strip())
s.close()
")"

ENABLED="$(echo "$STATUS_JSON2" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('enabled',False))")"
RUNTIME="$(echo "$STATUS_JSON2" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('runtime_enabled',False))")"

log "  enabled=$ENABLED"
log "  runtime_enabled=$RUNTIME"

[ "$ENABLED" = "True" ] || fail "overall enabled not True after toggle: $ENABLED"

log "✅ D.10a smoke test пройден"
log ""
log "Следующий шаг — manual test: диктуй через Right Option и проверь что текст"
log "попал с правильной пунктуацией. Смотри $LOG_FILE на 'LLM rewrite:' строки."
