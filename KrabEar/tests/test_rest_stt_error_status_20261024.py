"""Ошибка engine не является успешным прогревом; без MLX/HTTP сети."""
import io
import wave

import pytest

from test_rest_vg_contract_M1 import _deps_with
import backend.rest_server as rs


@pytest.fixture
def rest(monkeypatch, tmp_path):
    deps = _deps_with()
    deps.store.is_idempotent.return_value = False
    deps.store.call_privacy_mode.return_value = False
    deps.store.add_history_item.return_value.id = "fake-history"
    monkeypatch.setattr(rs, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    app = rs.create_app(deps)
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app.test_client(), deps, tmp_path


def post(client, call):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x10\0" * 4000)
    data = {"file": (io.BytesIO(output.getvalue()), "audio.wav")}
    if call:
        data.update(request_profile="voice_gateway_call", language="auto")
    return client.post("/v1/stt/transcribe", data=data)


@pytest.mark.parametrize("call", [False, True])
@pytest.mark.parametrize("result", [
    {"status": "error", "error": "PRIVATE_ENGINE_DETAIL", "text": "PRIVATE_TEXT"},
    {"status": "error", "text": ""},
    {"error": "PRIVATE_ENGINE_DETAIL", "text": ""},
])
def test_engine_error_is_not_success_or_history(rest, call, result):
    client, deps, temp = rest
    deps.transcriber.transcribe.return_value = result
    response = post(client, call)
    assert response.status_code == 503
    assert response.json == {"status": "error", "error": "stt_failed", "text": ""}
    assert response.headers.get("X-Request-ID")
    assert "PRIVATE_" not in response.get_data(as_text=True)
    deps.store.add_history_item.assert_not_called()
    deps.metrics.record.assert_not_called()
    assert list(temp.iterdir()) == []


@pytest.mark.parametrize("call", [False, True])
@pytest.mark.parametrize("result", [
    {"text": "", "reason": "vad_skip", "engine": "vad_skip"},
    {"text": "", "reason": "empty_audio", "engine": "empty_audio"},
    {"status": "ok", "error": None, "text": "ok", "engine": "fake"},
])
def test_legitimate_empty_and_normal_results_remain_success(rest, call, result):
    client, deps, _ = rest
    deps.transcriber.transcribe.return_value = result
    response = post(client, call)
    assert response.status_code == 200
    assert response.json["status"] == "ok"
    assert response.json["text"] == result["text"]
    deps.metrics.record.assert_called_once()


def test_call_privacy_still_wins_over_engine_error(rest):
    client, deps, _ = rest

    def failure(*args, **kwargs):
        deps.store.call_privacy_mode.return_value = True
        return {"status": "error", "error": "PRIVATE_ENGINE_DETAIL", "text": ""}

    deps.transcriber.transcribe.side_effect = failure
    response = post(client, True)
    assert response.status_code == 403
    assert response.json["skipped"] == "privacy_mode"
    assert "PRIVATE_" not in response.get_data(as_text=True)
    deps.store.add_history_item.assert_not_called()
