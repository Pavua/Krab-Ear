"""Общий IPC/REST TTS допуск: реальные handlers, синтетический audio backend."""

import io
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import backend.tts_service as tts


def _wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x01\0" * 100)
    return output.getvalue()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(tts.settings, "TTS_ENABLED", False)
    monkeypatch.setattr(tts.settings, "TTS_FALLBACK_SAY", True)
    monkeypatch.setattr(tts.settings, "SAY_VOICE", "")


def _failure(result, code):
    assert result.get("ok") is False
    assert result.get("error_code") == code
    assert result.get("wav_bytes_b64") == ""
    assert result.get("byte_count") == 0
    assert result.get("engine") == "none"


def test_concurrent_handlers_across_instances_reject_without_late_synthesis(enabled, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    audio = _wav()

    def backend(*_args, **_kwargs):
        entered.set()
        assert release.wait(3), "test must release synthetic backend"
        return audio

    synth = Mock(side_effect=backend)
    monkeypatch.setattr(tts, "_say_to_wav", synth)
    first, second = tts.TTSService(), tts.TTSService()
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(first.handle_synthesize_speech, {"text": "test one"})
        assert entered.wait(2)
        queued = pool.submit(second.handle_synthesize_speech, {"text": "test two"})
        try:
            result = queued.result(timeout=0.5)
            _failure(result, "tts_busy")
            assert synth.call_count == 1
        finally:
            release.set()
        assert active.result(timeout=2)["byte_count"] == len(audio)
        assert queued.result(timeout=2).get("error_code") == "tts_busy"
    assert synth.call_count == 1
    assert second.handle_synthesize_speech({"text": "test three"})["byte_count"] == len(audio)
    assert synth.call_count == 2


def test_enabled_empty_synthesis_is_unavailable(enabled, monkeypatch):
    monkeypatch.setattr(tts, "_say_to_wav", Mock(return_value=b""))
    _failure(tts.TTSService().handle_synthesize_speech({"text": "test"}), "tts_unavailable")


def test_backend_exception_is_unavailable_and_releases_admission(enabled, monkeypatch):
    synth = Mock(side_effect=[OSError("synthetic backend failure"), _wav()])
    monkeypatch.setattr(tts, "_say_to_wav", synth)
    service = tts.TTSService()
    _failure(service.handle_synthesize_speech({"text": "test"}), "tts_unavailable")
    assert service.handle_synthesize_speech({"text": "test again"})["byte_count"] > 0


def test_nonzero_say_does_not_start_converter(enabled, monkeypatch):
    runner = Mock(return_value=subprocess.CompletedProcess(["synthetic"], 1))
    monkeypatch.setattr(tts.subprocess, "run", runner)
    result = tts.TTSService().handle_synthesize_speech({"text": "test"})
    assert runner.call_count == 1, "failed say must not start afconvert"
    _failure(result, "tts_unavailable")


def test_nonzero_converter_does_not_return_partial_wav(enabled, monkeypatch):
    from pathlib import Path

    def run(args, **_kwargs):
        if args[0] == "afconvert":
            Path(args[-1]).write_bytes(_wav())
            return subprocess.CompletedProcess(["synthetic"], 1)
        return subprocess.CompletedProcess(["synthetic"], 0)

    monkeypatch.setattr(tts.subprocess, "run", run)
    _failure(tts.TTSService().handle_synthesize_speech({"text": "test"}), "tts_unavailable")


def test_say_timeout_is_unavailable_and_owned_files_are_removed(enabled, monkeypatch, tmp_path):
    original = tts.tempfile.NamedTemporaryFile
    monkeypatch.setattr(tts.tempfile, "NamedTemporaryFile", lambda **kw: original(dir=tmp_path, **kw))
    runner = Mock(side_effect=subprocess.TimeoutExpired("synthetic", 30))
    monkeypatch.setattr(tts.subprocess, "run", runner)
    _failure(tts.TTSService().handle_synthesize_speech({"text": "test"}), "tts_unavailable")
    assert list(tmp_path.iterdir()) == []
    assert runner.call_count == 1


def test_intentionally_disabled_synthesis_preserves_success_none(enabled, monkeypatch):
    monkeypatch.setattr(tts.settings, "TTS_FALLBACK_SAY", False)
    synth = Mock(side_effect=AssertionError("disabled must not invoke backend"))
    monkeypatch.setattr(tts, "_say_to_wav", synth)
    result = tts.TTSService().handle_synthesize_speech({"text": "test"})
    assert result.get("ok", True) is True
    assert result["engine"] == "none"
    assert result["byte_count"] == 0
    synth.assert_not_called()


@pytest.fixture
def rest(enabled, monkeypatch, tmp_path):
    from test_rest_vg_contract_M1 import _deps_with
    import backend.rest_server as rs

    deps = _deps_with()
    deps.tts_service = tts.TTSService()
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    monkeypatch.setattr(rs, "TEMP_DIR", tmp_path)
    app = rs.create_app(deps)
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app, deps


def test_rest_configured_failure_is_503_with_operational_code(rest, monkeypatch):
    app, _ = rest
    monkeypatch.setattr(tts, "_say_to_wav", Mock(return_value=b""))
    response = app.test_client().post("/v1/tts/synthesize", json={"text": "test"})
    assert response.status_code == 503
    assert response.json["error_code"] == "tts_unavailable"


def test_rest_busy_is_503_before_starting_another_backend(rest, monkeypatch):
    app, deps = rest
    entered, release = threading.Event(), threading.Event()

    def backend(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return _wav()

    synth = Mock(side_effect=backend)
    monkeypatch.setattr(tts, "_say_to_wav", synth)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(deps.tts_service.handle_synthesize_speech, {"text": "first"})
        assert entered.wait(2)
        try:
            response = app.test_client().post("/v1/tts/synthesize", json={"text": "second"})
            assert response.status_code == 503
            assert response.json["error_code"] == "tts_busy"
            assert synth.call_count == 1
        finally:
            release.set()
        assert active.result(timeout=2)["byte_count"] > 0


def test_rest_validation_and_disabled_contracts_survive(rest, monkeypatch):
    app, _ = rest
    assert app.test_client().post("/v1/tts/synthesize", json={"text": ""}).status_code == 400
    monkeypatch.setattr(tts.settings, "TTS_FALLBACK_SAY", False)
    response = app.test_client().post("/v1/tts/synthesize", json={"text": "test"})
    assert response.status_code == 200
    assert response.json["engine"] == "none"


def test_http_and_unix_ipc_share_admission_and_recover(rest, monkeypatch, tmp_path):
    """Настоящие listeners/dispatcher; заменён только синтез аудио и ML fixture."""
    import base64
    import json
    from http.client import HTTPConnection

    from werkzeug.serving import make_server
    from test_history_key_safety_ipc_20261014 import running_backend

    app, _ = rest
    entered, release = threading.Event(), threading.Event()
    audio = _wav()

    def backend(*_args, **_kwargs):
        entered.set()
        assert release.wait(5), "test must release HTTP synthesis"
        return audio

    synth = Mock(side_effect=backend)
    monkeypatch.setattr(tts, "_say_to_wav", synth)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    listener = threading.Thread(target=server.serve_forever, name="tts-http-fixture")

    def post():
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("POST", "/v1/tts/synthesize", json.dumps({"text": "synthetic"}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        with running_backend(tmp_path / "ipc") as (_, _, _, ipc):
            listener.start()
            with ThreadPoolExecutor(max_workers=1) as pool:
                active = pool.submit(post)
                try:
                    assert entered.wait(3)
                    busy = ipc("synthesize_speech", text="synthetic IPC")
                    assert busy["ok"] is True, "IPC transport succeeds; application reports busy"
                    _failure(busy["result"], "tts_busy")
                    busy_status, busy_result = post()
                    assert busy_status == 503
                    _failure(busy_result, "tts_busy")
                    assert synth.call_count == 1
                finally:
                    release.set()
                status, result = active.result(timeout=3)
                assert status == 200
                assert base64.b64decode(result["wav_bytes_b64"]) == audio
            assert post()[0] == 200
            recovered = ipc("synthesize_speech", text="synthetic IPC recovered")
            assert recovered["ok"] is True
            assert base64.b64decode(recovered["result"]["wav_bytes_b64"]) == audio
            assert synth.call_count == 3
            monkeypatch.setattr(tts, "_say_to_wav", Mock(return_value=b""))
            unavailable = ipc("synthesize_speech", text="synthetic unavailable")
            assert unavailable["ok"] is True
            _failure(unavailable["result"], "tts_unavailable")
    finally:
        release.set()
        if listener.ident is not None:
            server.shutdown()
            listener.join(timeout=3)
        server.server_close()
        assert not listener.is_alive()
