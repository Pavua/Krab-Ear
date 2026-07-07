#!/bin/bash
# run_e2e_bridge_smoke.command — двухпроцессный e2e-смок EventBridge (Волна 2).
#
# Поднимает THROWAWAY dev-backend (service.py) + rest_server.py на общий temp
# data-dir и СЛУЧАЙНЫЙ свободный порт (избегает конфликта с прод-REST на 5005,
# если он уже запущен через launchd). Проверяет: (1) нормальную доставку
# IPC->REST->SSE <=200мс, (2) хаос-кейс (REST убит -> IPC не блокируется),
# (3) восстановление (REST поднят заново -> новое событие доходит), (4)
# поправка контролёра №2 (2026-07-07): realtime.partial_transcript (5-я жертва
# гэпа — streaming paste) проходит через мост, не только krab_error.
#
# Exit 0 только если ВСЕ четыре фазы прошли.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

VENV="$REPO/.venv_krab_ear"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "ERROR: venv python не найден: $PY"; exit 1; }

DATADIR="$(mktemp -d /tmp/krab_ear_bridge_e2e.XXXXXX)"
SOCK="$DATADIR/krabear.sock"

# Свободный порт — не хардкодим 5005, чтобы не конфликтовать с реальным launchd REST.
REST_PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

# Поправка контролёра №2 (T6, обязательное дополнение): throwaway IPC-процесс
# запускается через маленький генерируемый driver вместо голого KrabEar/main.py.
# Driver вызывает РЕАЛЬНЫЙ backend.service.main() (та же проводка EventBridge,
# что и в проде) + планирует ОДИН синтетический event_bus.emit(
# "realtime.partial_transcript", ...) через RT_PARTIAL_DELAY_SEC после старта —
# доказывает, что мост доставляет ИМЕННО этот тип события (5-я жертва гэпа,
# StreamingPasteController.swift:121), не запуская реальный захват микрофона
# (недетерминированно + приватность-инвазивно для безнадзорного smoke-теста) и
# не добавляя новый прод-IPC метод. Файл генерируется в $DATADIR (не коммитится,
# удаляется в cleanup()).
RT_PARTIAL_DELAY_SEC=10
IPC_DRIVER="$DATADIR/_ipc_driver.py"
cat > "$IPC_DRIVER" <<PYEOF
"""_ipc_driver.py — throwaway e2e driver (сгенерирован run_e2e_bridge_smoke.command).

Запускает РЕАЛЬНЫЙ backend.service.main() (идентичная прод-проводка
EventBridge) и планирует один синтетический event_bus.emit(
"realtime.partial_transcript", ...) через ${RT_PARTIAL_DELAY_SEC}с после
старта — proof that this SPECIFIC event type crosses the bridge (controller
amendment #2, T6), без реального захвата микрофона.
"""
import sys
import threading
import time

sys.argv = ["main.py", "--data-dir", "${DATADIR}"]

from backend.event_bus import bus as event_bus  # noqa: E402
from backend.service import main as service_main  # noqa: E402


def _emit_synthetic_partial() -> None:
    time.sleep(${RT_PARTIAL_DELAY_SEC})
    event_bus.emit(
        "realtime.partial_transcript",
        {
            "session_id": "e2e-bridge-smoke",
            "text": "e2e-bridge-smoke synthetic partial transcript",
            "is_partial": True,
            "ts": time.time(),
        },
    )


threading.Thread(target=_emit_synthetic_partial, daemon=True).start()
service_main()
PYEOF

IPC_PID=""; REST_PID=""; RT_PID=""
cleanup() {
  for pid in "$IPC_PID" "$REST_PID"; do
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null
  done
  sleep 1
  for pid in "$IPC_PID" "$REST_PID"; do
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
  done
  rm -rf "$DATADIR"
}
trap cleanup EXIT INT TERM

start_ipc() {
  # ФИКС (обнаружено при живом прогоне T6): без PYTHONUNBUFFERED=1 stdout/stderr,
  # редиректнутые в файл (не tty), block-buffered — свежие log-строки (в т.ч.
  # "EventBridge: REST недоступен") сидят в буфере процесса и НЕ попадают в
  # ipc.log до заполнения буфера или чистого выхода; принудительный kill в конце
  # (или между фазами) их теряет. Отсюда ложные "0 WARN найдено" при живом grep.
  PYTHONPATH="$REPO/KrabEar" KRAB_EAR_REST_SERVER_PORT="$REST_PORT" PYTHONUNBUFFERED=1 \
    "$PY" "$IPC_DRIVER" > "$DATADIR/ipc.log" 2>&1 &
  IPC_PID=$!
  for _ in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.5; done
  [ -S "$SOCK" ] || { echo "FATAL: IPC-сокет не появился"; tail -30 "$DATADIR/ipc.log"; exit 1; }
  sleep 2
}

start_rest() {
  KRAB_EAR_DATA_DIR="$DATADIR" KRAB_EAR_REST_SERVER_PORT="$REST_PORT" PYTHONUNBUFFERED=1 \
    PYTHONPATH="$REPO/KrabEar" "$PY" KrabEar/backend/rest_server.py \
    > "$DATADIR/rest.log" 2>&1 &
  REST_PID=$!
  for _ in $(seq 1 40); do
    curl -s -o /dev/null "http://127.0.0.1:$REST_PORT/health" && break
    sleep 0.5
  done
}

echo "==> data-dir=$DATADIR rest-port=$REST_PORT"
echo "==> Запуск IPC-процесса (driver, планирует realtime.partial_transcript через ${RT_PARTIAL_DELAY_SEC}с)"
start_ipc
echo "==> Запуск REST-процесса"
start_rest

rc=0

echo ""
echo "==> Фаза 0 (поправка контролёра №2): слушаем realtime.partial_transcript в фоне"
echo "    (запущено РАНО — драйвер эмитит его через ${RT_PARTIAL_DELAY_SEC}с от старта IPC)"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_event_bridge_smoke.py "$SOCK" "http://127.0.0.1:$REST_PORT" realtime_partial &
RT_PID=$!

echo ""
echo "==> Фаза 1: нормальная доставка (latency <=200мс)"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_event_bridge_smoke.py "$SOCK" "http://127.0.0.1:$REST_PORT" normal || rc=1

echo ""
echo "==> Ждём завершения Фазы 0 (realtime.partial_transcript)"
wait "$RT_PID" || rc=1
RT_PID=""

echo ""
echo "==> Хаос: убиваем REST-процесс"
kill -TERM "$REST_PID" 2>/dev/null; sleep 1; kill -KILL "$REST_PID" 2>/dev/null; REST_PID=""

echo "==> Фаза 2: IPC не блокируется при мёртвом REST"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_event_bridge_smoke.py "$SOCK" "http://127.0.0.1:$REST_PORT" after-kill || rc=1

# Проверяем ровно один WARN о смене состояния (down) в IPC-логе.
# ФИКС (обнаружено при живом прогоне T6): on_event()/wake_event.set() будит
# sender-тред немедленно, но фактическая неудачная POST-попытка (TCP-connect
# refused + логирование) выполняется АСИНХРОННО в отдельном потоке — grep
# сразу после IPC-вызова гонится с этим логированием. Poll вместо one-shot.
down_warns=0
for _ in $(seq 1 10); do
  down_warns=$(grep -c "EventBridge: REST недоступен" "$DATADIR/ipc.log" || true)
  [ "$down_warns" -ge 1 ] && break
  sleep 0.5
done
if [ "$down_warns" -ne 1 ]; then
  echo "FAIL: ожидался 1 WARN о переходе в down, найдено: $down_warns"
  rc=1
else
  echo "OK: ровно 1 WARN о смене состояния (down)"
fi

echo ""
echo "==> Восстановление: поднимаем REST заново на том же порту/data-dir"
start_rest

echo "==> Фаза 3: событие доходит после восстановления (<= ~35с backoff-потолок + запас)"
# ФИКС (обнаружено при живом прогоне T6): комментарий в e2e_event_bridge_smoke.py
# (after-recovery) предполагает, что оркестратор триггерит НОВОЕ событие ПОСЛЕ
# того, как SSE начал слушать — но версия плана этого не делала, из-за чего
# авто-ретрай backlog-события из Фазы 2 мог уйти ДО регистрации SSE-подписчика
# этой фазы (SSE не имеет replay — подписчика ещё не существовало в момент
# доставки). Фикс: запускаем SSE-слушатель В ФОНЕ, даём ему зарегистрироваться,
# затем явно триггерим свежее событие (report_paste_failure с другим маркером).
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_event_bridge_smoke.py "$SOCK" "http://127.0.0.1:$REST_PORT" after-recovery &
AFTER_RECOVERY_PID=$!
sleep 1
"$PY" - "$SOCK" <<'PYEOF'
import json
import socket
import sys

sock_path = sys.argv[1]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
s.connect(sock_path)
req = json.dumps({
    "id": "bridge-smoke-recovery-trigger",
    "method": "report_paste_failure",
    # ФИКС (обнаружено при живом прогоне T6): ErrorBus.push() дедупит повторный
    # push ТОГО ЖЕ error-кода в 30-секундном окне (error_bus.py, 30.0s default) —
    # "ax_denied" уже был запушен Фазой 1 секундами раньше в этом же прогоне;
    # "app_unsupported" — другой код (paste.app_unsupported), не дедуплицируется.
    "params": {"reason": "app_unsupported", "app_bundle": "com.test.e2e.recovery"},
}) + "\n"
s.sendall(req.encode("utf-8"))
buf = b""
while b"\n" not in buf:
    chunk = s.recv(65536)
    if not chunk:
        break
    buf += chunk
s.close()
print("trigger after recovery:", buf.decode("utf-8"))
PYEOF
wait "$AFTER_RECOVERY_PID" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "============================================================"
  echo "  EVENT BRIDGE E2E: ALL GREEN"
  echo "============================================================"
else
  echo "============================================================"
  echo "  EVENT BRIDGE E2E: FAILURE — см. вывод выше; логи в $DATADIR"
  echo "============================================================"
  cp "$DATADIR/ipc.log" "/tmp/krab_ear_bridge_e2e_ipc_last_failure.log" 2>/dev/null
  cp "$DATADIR/rest.log" "/tmp/krab_ear_bridge_e2e_rest_last_failure.log" 2>/dev/null
  cp "$DATADIR/backend.log" "/tmp/krab_ear_bridge_e2e_backend_last_failure.log" 2>/dev/null
fi
exit "$rc"
