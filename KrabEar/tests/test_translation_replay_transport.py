"""Реальный Unix transport: dispatcher/auth/journal и потерянный commit ACK."""
import json
import socket
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.event_replay import EventReplayManager


@pytest.fixture
def peer(tmp_path):
    from backend.translation_replay_ingress import TranslationReplayIngress
    from backend.translation_replay_client import TranslationReplayClient
    from backend.service import BackendService
    from backend.ipc_throttle import IPCThrottle
    manager = EventReplayManager()
    ingress = TranslationReplayIngress(manager, tmp_path)
    service = BackendService.__new__(BackendService)
    service._request_signer = None
    service._ipc_throttle = IPCThrottle()
    service._audit_logger = Mock()
    service._settings_svc = SimpleNamespace(cached_settings=lambda: {})
    service._dispatch_table = {"translation_replay_begin": ingress.begin,
                               "translation_replay_commit": ingress.commit,
                               "get_event_log": manager.handle_get_event_log}
    socket_dir = Path(tempfile.mkdtemp(prefix="ker-", dir="/tmp"))
    path = socket_dir / "p.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(8)
    server.settimeout(.1)
    stop = threading.Event()
    state = {"lose_ack": False}
    errors = []
    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            try:
                with conn, conn.makefile("rb") as stream:
                    request = json.loads(stream.readline(1024 * 1024))
                    response = service.handle_request(request)
                    if request["method"] == "translation_replay_commit" and state["lose_ack"]:
                        state["lose_ack"] = False
                        continue
                    conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode())
            except Exception as exc:
                errors.append(type(exc).__name__)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield manager, TranslationReplayClient(path, tmp_path), state
    finally:
        stop.set()
        thread.join(2)
        server.close()
        path.unlink()
        socket_dir.rmdir()
        assert not thread.is_alive() and not errors


def phrase():
    return {"text": "повтор", "translation": "repeat", "start_ts": 0., "end_ts": 3., "language_detected": "ru"}


def test_real_dispatcher_lost_ack_and_rest_cursor_repair(peer):
    manager, client, state = peer
    assert client.open()["status"] == "ready"
    old = client.replay()["cursor"]
    for _ in range(2):
        ticket = client.begin_window()
        state["lose_ack"] = True
        assert client.commit_window(ticket, phrase())["status"] == "duplicate"
    first = client.replay(old, limit=1)
    second = client.replay(first["cursor"])
    assert first["has_more"] and not second["has_more"]
    assert [e["data"]["text"] for p in (first, second) for e in p["events"]] == ["повтор", "повтор"]
    assert len(manager._buffer) == 2
    manager.clear()
    assert client.begin_window()["status"] == "stale"


def test_large_mixed_transport_pages_never_stall_cursor(peer):
    manager, client, _ = peer
    client.open()
    old = client.replay()["cursor"]
    for _ in range(100):
        manager.record_event("live_subs.result", {"text": "x" * 12000})
        ticket = client.begin_window()
        assert client.commit_window(ticket, {**phrase(), "text": "y" * 12000})["status"] == "accepted"
    count = 0
    for _ in range(20):
        page = client.replay(old)
        assert page["cursor"] != old
        old = page["cursor"]
        count += len(page["events"])
        if not page["has_more"]:
            break
    assert count == 200


def test_actual_rest_ws_producer_then_http_consumer_replays_without_echo(peer, monkeypatch):
    import base64
    import urllib.request
    from werkzeug.serving import make_server
    from simple_websocket import Client, ConnectionClosed
    from backend.event_bus import EventBus
    import backend.live_subs_service as live
    import backend.rest_server as rs
    from test_rest_vg_contract_M1 import _deps_with
    manager, replay_client, state = peer
    deps = _deps_with()
    deps.transcriber.transcribe.return_value = {"text": "повтор", "language": "ru"}
    bus = EventBus()
    bus._event_replay = manager  # Любой повторный emit сразу даст лишнюю запись.
    monkeypatch.setattr(live, "event_bus", bus)
    monkeypatch.setattr(rs, "_translation_replay_client", lambda: replay_client)
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    app = rs.create_app(deps)
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def get(cursor=None):
        url = base + "/v1/translation/replay"
        if cursor:
            url += "?cursor=" + cursor
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.load(response)
    try:
        old = get()["cursor"]
        for _ in range(2):
            ws = Client.connect(base.replace("http:", "ws:") + "/v1/stream")
            try:
                ws.send(json.dumps({"type": "config", "backend": "auto", "mode": "translate", "target_lang": "off"}))
                state["lose_ack"] = True
                ws.send(json.dumps({"type": "audio", "data": base64.b64encode(bytes(32000)).decode(),
                                    "sample_rate": 16000, "is_final": True}))
                result = json.loads(ws.receive(timeout=3))
                assert result["text"] == "повтор" and result["replay_status"] == "duplicate"
                assert len(result["event_id"]) == 32
            finally:
                try:
                    ws.close()
                except (ConnectionClosed, OSError):
                    pass  # Peer закрыл final WS между receive и локальным close.
                finally:
                    ws.sock.close()
        page = get(old)
        assert [e["data"]["text"] for e in page["events"]] == ["повтор", "повтор"]
        assert len(manager._buffer) == 2
        assert get(page["cursor"])["events"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()
