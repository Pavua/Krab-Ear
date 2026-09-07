"""Независимые контрпримеры privacy/deadline для телефонного REST-профиля."""

import io
from pathlib import Path
from unittest.mock import Mock

import pytest

import backend.call_stt_client as call_client
import backend.rest_server as rs
import test_rest_call_stt_profile as profile_tests

_post = profile_tests._post
rest = profile_tests.rest


def test_unreadable_privacy_wins_before_invalid_rest_auth(rest, monkeypatch):
    client, deps = rest
    deps.store.call_privacy_mode.side_effect = OSError("settings are unavailable")
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "test-only-key")
    ipc = Mock(side_effect=AssertionError("must not send"))
    monkeypatch.setattr(call_client, "transcribe_ephemeral_call", ipc)
    assert _post(client).status_code == 403
    ipc.assert_not_called()
    deps.transcriber.transcribe.assert_not_called()


@pytest.mark.parametrize("error_type", [call_client.CallSTTTimeoutError, call_client.CallSTTProtocolError])
def test_privacy_toggle_during_owner_transport_failure_forbids_fallback(rest, monkeypatch, error_type):
    client, deps = rest

    def ipc(*args, **kwargs):
        deps.store.call_privacy_mode.return_value = True
        raise error_type("transport failed after privacy changed")

    monkeypatch.setattr(call_client, "transcribe_ephemeral_call", ipc)
    assert _post(client).status_code == 403
    deps.transcriber.transcribe.assert_not_called()


def test_deadline_includes_initial_privacy_read(rest, monkeypatch):
    client, deps = rest
    now = [100.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: now[0])
    reads = 0

    def settings(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            now[0] = 109.0
        return False

    deps.store.call_privacy_mode.side_effect = settings
    ipc = Mock(return_value={"status": "ok", "text": "привет"})
    monkeypatch.setattr(call_client, "transcribe_ephemeral_call", ipc)
    response = _post(client, deadline_sec="8")
    assert response.status_code == 504
    ipc.assert_not_called()


def test_completed_local_future_after_deadline_cannot_return_success(rest, monkeypatch):
    client, deps = rest
    now = [100.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: now[0])

    def transcribe(*args, **kwargs):
        now[0] = 109.0
        return {"text": "hola", "language": "es", "engine": "mlx-whisper"}

    deps.transcriber.transcribe.side_effect = transcribe
    response = _post(client, "auto", deadline_sec="8")
    assert response.status_code == 504
    assert not response.json.get("text")
    deps.store.add_history_item.assert_not_called()


@pytest.mark.parametrize("outcome", ["busy", "error"])
def test_privacy_toggle_during_local_failure_forbids_fallback(rest, monkeypatch, outcome):
    client, deps = rest

    def fail(*args, **kwargs):
        deps.store.call_privacy_mode.return_value = True
        if outcome == "busy":
            return False
        raise RuntimeError("local inference failed")

    if outcome == "busy":
        monkeypatch.setattr(rs, "try_acquire_stt_singleflight", fail)
    else:
        deps.transcriber.transcribe.side_effect = fail
    assert _post(client, "auto").status_code == 403
    deps.store.add_history_item.assert_not_called()


@pytest.mark.parametrize("override", [None, "/tmp/review-explicit-owner.sock"])
def test_owner_ipc_uses_authoritative_signing_and_socket_config(rest, monkeypatch, tmp_path, override):
    import backend.service as service

    client, deps = rest
    monkeypatch.setattr(service, "default_data_dir", lambda: tmp_path / "owner-state")
    monkeypatch.setattr(rs.settings, "DATA_DIR", tmp_path / "different-rest-state")
    monkeypatch.setattr(rs.settings, "IPC_SIGNING_ENABLED", True)
    monkeypatch.setattr(rs.settings, "IPC_SIGNING_SECRET", "test-only-secret")
    if override is None:
        monkeypatch.delenv("KRAB_EAR_SOCKET", raising=False)
    else:
        monkeypatch.setenv("KRAB_EAR_SOCKET", override)
    ipc = Mock(return_value={"status": "ok", "text": "привет"})
    monkeypatch.setattr(call_client, "transcribe_ephemeral_call", ipc)
    response = _post(client, signing_enabled="false", signing_secret="untrusted-form", socket_path="/tmp/form.sock")
    assert response.status_code == 200
    params = ipc.call_args.kwargs
    assert params["signing_enabled"] is True
    assert params["signing_secret"] == "test-only-secret"
    assert params["socket_path"] == (
        Path(override) if override else tmp_path / "owner-state" / "krabear.sock"
    )


def test_oversize_call_upload_is_rejected_before_second_disk_copy(rest, monkeypatch):
    from backend.call_stt_wire import CALL_MAX_WAV_BYTES
    from werkzeug.datastructures import FileStorage

    client, deps = rest
    saved = []
    original_save = FileStorage.save

    def save(file, *args, **kwargs):
        saved.append(file.filename)
        return original_save(file, *args, **kwargs)

    monkeypatch.setattr(FileStorage, "save", save)
    response = client.post("/v1/stt/transcribe", data={
        "file": (io.BytesIO(b"x" * (CALL_MAX_WAV_BYTES + 1)), "audio.wav"),
        "request_profile": "voice_gateway_call", "language": "auto",
    })
    assert response.status_code in (400, 413)
    assert saved == [], "bounded call profile copied oversized upload before validation"
    deps.transcriber.transcribe.assert_not_called()


def test_privacy_wins_when_rate_limit_is_exhausted(rest, monkeypatch):
    client, deps = rest
    deps.store.call_privacy_mode.return_value = True
    client.application.config["RATELIMIT_ENABLED"] = True
    monkeypatch.setattr(rs.limiter, "enabled", True)
    client.environ_base["REMOTE_ADDR"] = "127.255.43.91"
    for request_number in range(61):
        response = _post(client)
        assert response.status_code == 403, f"request {request_number + 1} bypassed privacy through rate limit"
    deps.transcriber.transcribe.assert_not_called()
