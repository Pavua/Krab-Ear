#!/bin/bash
# e2e Call Observer w1: fake VG + интеграционные XCTest.
# Bash 3.2-совместимо (macOS): без mapfile/timeout.
set -u
cd "$(dirname "$0")/.."

PORT=18090
PYTHON=".venv_krab_ear/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

"$PYTHON" scripts/fake_vg_server.py "$PORT" >/tmp/fake_vg_server.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

# Ждём готовность сервера (fail-closed: не дождались → красный выход).
READY=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:$PORT/v1/sessions" >/dev/null 2>&1; then
        READY=1; break
    fi
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    echo "FAIL: fake VG не поднялся (см. /tmp/fake_vg_server.log)" >&2
    exit 1
fi

cd native/KrabEarAgent
KRAB_E2E_VG_PORT="$PORT" swift test --filter CallObserverE2ETests
RC=$?
exit $RC
