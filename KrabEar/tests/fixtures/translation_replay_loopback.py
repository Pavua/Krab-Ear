"""Synthetic IPC+HTTP peer для Swift component E2E; production не запускает."""

from __future__ import annotations
import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.event_replay import EventReplayManager
from backend.translation_replay_ingress import TranslationReplayIngress
from backend.translation_replay_client import TranslationReplayClient

root = Path(sys.argv[1])
manager = EventReplayManager()
ingress = TranslationReplayIngress(manager, root)
rest_records = 0
manager.record_event("live_subs.result", {"text": "до обрыва", "translation": "before"})
ipc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
ipc.bind(str(root / "ipc.sock"))
ipc.listen(4)
http = socket.socket()
http.bind(("127.0.0.1", 0))
http.listen(4)
first_read = threading.Event()


def rpc():
    while True:
        conn, _ = ipc.accept()
        with conn, conn.makefile("rb") as stream:
            request = json.loads(stream.readline(65536))
            handlers = {"get_event_log": manager.handle_get_event_log,
                        "translation_replay_begin": ingress.begin,
                        "translation_replay_commit": ingress.commit}
            result = handlers[request["method"]](request.get("params", {}))
            response = {"id": request["id"], "ok": True, "result": result}
            conn.sendall((json.dumps(response) + "\n").encode())
            first_read.set()


def ready(connections):
    temporary = root / "ready.tmp"
    temporary.write_text(
        json.dumps({"port": http.getsockname()[1], "connections": connections, "rest_records": rest_records})
    )
    temporary.replace(root / "ready.json")


threading.Thread(target=rpc, daemon=True).start()
ready(0)
for count in (1, 2):
    conn, _ = http.accept()
    with conn:
        conn.settimeout(5)
        request = conn.recv(8192)
        if b"/v1/events?filter=live_subs.result" not in request:
            raise RuntimeError("unexpected synthetic SSE route")
        if not first_read.wait(5):
            raise RuntimeError("initial replay read was not observed")
        ready(count)
        body = b'data: {"text":"raw SSE must not duplicate replay"}\n\n'
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            + body
        )
        if count == 1:
            conn.shutdown(socket.SHUT_RDWR)
        else:
            # Оставляем второй stream живым до cleanup владельца.
            try:
                conn.recv(1)
            except socket.timeout:
                pass
    if count == 1:
        # Эти две легитимные одинаковые фразы появились после закрытия TCP.
        producer = TranslationReplayClient(root / "ipc.sock", root)
        assert producer.open()["status"] == "ready"
        for _ in range(2):
            ticket = producer.begin_window()
            result = producer.commit_window(ticket, {"text": "повтор", "translation": "repeat",
                "start_ts": 0., "end_ts": 3., "language_detected": "ru"})
            assert result["status"] == "accepted"
            rest_records += 1
threading.Event().wait(10)
