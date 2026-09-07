"""Call-profile REST: RU идёт владельцу, auto/ES эфемерны, один deadline."""
import io
import wave
from unittest.mock import Mock

import pytest

from test_rest_vg_contract_M1 import _deps_with
import backend.rest_server as rs
import backend.call_stt_client as call_client


def _wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x10\0" * 8000)
    return output.getvalue()


@pytest.fixture
def rest(monkeypatch, tmp_path):
    deps = _deps_with()
    deps.store.call_privacy_mode.return_value = False
    deps.store.is_idempotent.return_value = False
    deps.transcriber.transcribe.return_value = {
        "text": "hola", "language": "es", "engine": "mlx-whisper",
    }
    monkeypatch.setattr(rs, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    app = rs.create_app(deps)
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app.test_client(), deps


def _post(client, language="ru", **extra):
    return client.post("/v1/stt/transcribe", data={
        "file": (io.BytesIO(_wav()), "audio.wav"),
        "request_profile": "voice_gateway_call", "language": language,
        **extra,
    })


def test_ru_profile_bypasses_standalone_engine(rest, monkeypatch):
    client, deps = rest
    ipc = Mock(return_value={
        "status": "ok", "text": "привет", "engine": "gigaam-rnnt",
        "mode": "v3_e2e_rnnt", "language": "ru", "transport": "subprocess",
    })
    monkeypatch.setattr(call_client, "transcribe_ephemeral_call", ipc)
    response = _post(client, persist_history="true")
    assert response.status_code == 200
    assert response.json["text"] == "привет"
    assert response.json["mode"] == "v3_e2e_rnnt"
    ipc.assert_called_once()
    deps.engine.normalize_audio.assert_not_called()
    deps.transcriber.transcribe.assert_not_called()
    deps.store.add_history_item.assert_not_called()


@pytest.mark.parametrize("language", ["auto", "es", "en"])
def test_all_call_profiles_are_context_free_and_never_persist(rest, language):
    client, deps = rest
    response = _post(client, language=language, persist_history="true")
    assert response.status_code == 200
    kwargs = deps.transcriber.transcribe.call_args.kwargs
    assert kwargs["lang_hint"] == language
    assert kwargs["context_free"] is True and kwargs["single_pass"] is True
    assert kwargs["diarize"] is False
    assert kwargs["extra_vocabulary"] == []
    deps.store.add_history_item.assert_not_called()


@pytest.mark.parametrize("status,http", [("privacy_mode",403), ("timeout",504), ("busy",503), ("not_ready",503)])
def test_owner_outcome_mapping(rest, monkeypatch, status, http):
    client, deps = rest
    monkeypatch.setattr(call_client, "transcribe_ephemeral_call", Mock(return_value={
        "status": status, "text": "", "reason": status,
    }))
    assert _post(client).status_code == http
    deps.transcriber.transcribe.assert_not_called()


def test_owner_privacy_unreadable_fails_closed_before_ipc(rest, monkeypatch):
    client, deps = rest
    deps.store.call_privacy_mode.side_effect = OSError("busy")
    ipc = Mock(side_effect=AssertionError("must not send"))
    monkeypatch.setattr(call_client, "transcribe_ephemeral_call", ipc)
    assert _post(client).status_code == 403
    ipc.assert_not_called()


def test_general_profile_busy_does_not_wait_a_second_deadline(rest, monkeypatch):
    client, deps = rest
    acquire = Mock(return_value=False)
    monkeypatch.setattr(rs, "try_acquire_stt_singleflight", acquire)
    response = _post(client, "auto", deadline_sec="8")
    assert response.status_code == 503
    acquire.assert_called_once_with(0.0)
    deps.transcriber.transcribe.assert_not_called()


def test_profile_deadline_includes_audio_preparation(rest, monkeypatch):
    client, deps = rest
    now = [100.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: now[0])
    deps.engine.normalize_audio.side_effect = lambda *_: now.__setitem__(0, 109.0)
    response = _post(client, "auto", deadline_sec="8")
    assert response.status_code == 504
    deps.transcriber.transcribe.assert_not_called()


def test_unknown_profile_rejected(rest):
    client, deps = rest
    response = _post(client, request_profile="invented")
    assert response.status_code == 400
    deps.transcriber.transcribe.assert_not_called()
