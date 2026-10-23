"""REST consumer repair использует общий cursor, auth/origin/privacy gates."""
from unittest.mock import Mock

import pytest
import backend.rest_server as rs
from backend.event_replay import EventReplayManager

pytest_plugins = ["test_rest_call_stt_profile"]


def test_rest_consumer_repairs_two_identical_phrases_after_disconnect(rest, monkeypatch):
    client, deps = rest
    manager = EventReplayManager()
    proxy = Mock()
    proxy.replay.side_effect = lambda cursor=None, limit=200: manager.handle_get_event_log(
        {"cursor_mode": True, "event_type": "live_subs.result", "cursor": cursor, "limit": limit})
    monkeypatch.setattr(rs, "_translation_replay_client", lambda: proxy)
    first = client.get("/v1/translation/replay")
    assert first.status_code == 200
    old = first.json["cursor"]
    for _ in range(2):
        manager.record_event("live_subs.result", {"text": "повтор", "translation": "repeat"})
    page = client.get("/v1/translation/replay", query_string={"cursor": old, "limit": 1})
    assert page.status_code == 200 and page.json["has_more"]
    next_page = client.get("/v1/translation/replay", query_string={"cursor": page.json["cursor"]})
    assert next_page.status_code == 200
    assert [e["data"]["text"] for p in (page, next_page) for e in p.json["events"]] == ["повтор", "повтор"]
    assert not client.get("/v1/translation/replay", query_string={"cursor": next_page.json["cursor"]}).json["events"]


@pytest.mark.parametrize("case", ["auth", "origin", "privacy", "settings_error"])
def test_replay_read_gates_precede_ipc(rest, monkeypatch, case):
    client, deps = rest
    factory = Mock()
    monkeypatch.setattr(rs, "_translation_replay_client", factory)
    headers = {}
    if case == "auth":
        monkeypatch.setattr(rs.settings, "REST_API_KEY", "synthetic-key")
    elif case == "origin":
        headers["Origin"] = "https://foreign.invalid"
    elif case == "privacy":
        deps.store.load_settings.return_value = {"privacy_mode_enabled": True}
    else:
        deps.store.load_settings.side_effect = OSError("unavailable")
    assert client.get("/v1/translation/replay", headers=headers).status_code in (401, 403)
    factory.assert_not_called()


def test_privacy_during_ws_handshake_cannot_silently_disable_replay(rest, monkeypatch):
    import json
    import base64
    client, deps = rest
    deps.transcriber.transcribe.return_value = {"text": "private phrase", "language": "ru"}
    class WS:
        def __init__(self):
            self.messages = [json.dumps({"type": "config"}), json.dumps({"type": "audio",
                                                                         "data": base64.b64encode(bytes(32000)).decode(), "is_final": True})]
            self.sent = []
        def receive(self):
            return self.messages.pop(0) if self.messages else None
        def send(self, value):
            self.sent.append(json.loads(value))
        def close(self, **kwargs):
            pass
    ws = WS()
    calls = iter([False, True])
    monkeypatch.setattr(rs, "_privacy_mode_enabled", lambda: next(calls, False))
    with client.application.test_request_context("/v1/stream"):
        rs._ws_stream_handler(ws)
    assert ws.sent and ws.sent[0].get("code") == "privacy_mode_active"
    deps.transcriber.transcribe.assert_not_called()


@pytest.mark.parametrize("separate_socket", [False, True])
def test_factory_uses_explicit_ipc_owner_profile_for_token_and_socket(tmp_path, monkeypatch, separate_socket):
    from backend.translation_replay_ingress import TranslationReplayIngress
    import backend.service as service
    default = tmp_path / "default"
    owner = tmp_path / "owner"
    default.mkdir()
    owner.mkdir()
    TranslationReplayIngress(EventReplayManager(), default)
    ingress = TranslationReplayIngress(EventReplayManager(), owner)
    monkeypatch.setattr(service, "default_data_dir", lambda: default)
    monkeypatch.setenv("KRAB_EAR_IPC_DATA_DIR", str(owner))
    if separate_socket:
        socket_path = tmp_path / "separate.sock"
        monkeypatch.setenv("KRAB_EAR_SOCKET", str(socket_path))
    else:
        socket_path = owner / "krabear.sock"
        monkeypatch.delenv("KRAB_EAR_SOCKET", raising=False)
    monkeypatch.setattr(rs.settings, "IPC_SIGNING_ENABLED", False)
    c = rs._translation_replay_client()
    assert c._path == socket_path
    c._rpc = lambda method, params, deadline: ingress.begin(params)
    assert c.open()["status"] == "ready"
