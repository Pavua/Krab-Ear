"""STT WebSocket должен учитываться до завершения обработки аудио."""
import socket
from threading import Event
from types import SimpleNamespace

from flask import Flask
from flask_sock import Sock
from websockets.sync.client import connect

from backend.rest_inprocess import InProcessRestServer


def test_live_stt_websocket_is_counted_until_handler_returns():
    app = Flask(__name__)
    sock = Sock(app)
    entered = Event()

    @sock.route("/v1/stream")
    def stream(ws):
        entered.set()
        ws.receive(timeout=5)

    server = InProcessRestServer(app, SimpleNamespace(REST_SERVER_PORT=0), True)
    server._start_whisper_idle_reaper = lambda: None
    assert server.start()
    port = server._server.server_port
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw_socket:
            with connect(
                f"ws://127.0.0.1:{port}/v1/stream", sock=raw_socket, open_timeout=5
            ) as ws:
                assert entered.wait(5), "WebSocket handler did not start"
                with server._active_lock:
                    active = server._active_requests
                assert active == 1, "live STT WebSocket missing from active requests"
                assert not server._drain_active_requests(0), "live stream reported drained"
                ws.send("end")
        assert server._drain_active_requests(5), "finished WebSocket remained counted"
    finally:
        server.stop()
