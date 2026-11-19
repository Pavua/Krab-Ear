"""Регрессии REST/STT с синтетическим аудио и заменой границы моделей."""
import io
import signal
import threading
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import rest_server as rest
from backend.transcriber import Transcriber
from core import engine as audio_engine
from core import mlx_whisper_session as whisper
from core import stt_budget
from core.mlx_subprocess import MLXTimeoutError


@pytest.fixture
def client_engine(monkeypatch, tmp_path):
    settings = audio_engine.settings
    for key in (
        "STT_GIGAAM_ENABLED", "STT_USE_RU_FINETUNE", "PARAKEET_ENABLED",
        "SENSEVOICE_ENABLED", "WHISPERX_ENABLED", "VOXTRAL_ENABLED",
        "PIPELINE_V2", "STT_MULTIPASS_ENABLED", "STT_STREAMING_ENABLED",
        "REST_API_AUTH_ENABLED",
    ):
        monkeypatch.setattr(settings, key, False)
    monkeypatch.setattr(settings, "NETWORK_MODE", "offline_strict")
    monkeypatch.setattr(settings, "REST_API_KEY", "")
    monkeypatch.setattr(settings, "MODEL_BALANCED", "synthetic-whisper")
    monkeypatch.setattr(settings, "MODEL_MAX_CANDIDATES", "synthetic-whisper")
    monkeypatch.setattr(settings, "MLX_TRANSCRIBE_TIMEOUT_SEC", 60.0)
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "1")
    monkeypatch.setenv("KRAB_EAR_MLX_INTER_PROCESS_LOCK", "0")
    monkeypatch.setattr(rest, "TEMP_DIR", tmp_path)
    # Все тесты могут закрывать ответ с вооружённым call_on_close.
    abort_rest = Mock()
    monkeypatch.setattr(rest, "_exit_poisoned_rest_process", abort_rest)
    engine = audio_engine.AudioEngine(skip_gigaam_warmup=True)
    store = Mock()
    store.load_settings.return_value = {}
    store.load_vocabulary.return_value = []
    deps = rest.StaticDeps(
        engine=engine, store=store, transcriber=Transcriber(engine),
        translator=Mock(), tts_service=Mock(), metrics=Mock(),
        event_bus=Mock(), sse_stream=Mock(),
    )
    app = rest.create_app(deps, {"TESTING": True, "RATELIMIT_ENABLED": False})
    yield app.test_client(), engine, abort_rest
    engine.close()


def post(client, **fields):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x10" * 16000)
    data = {
        "file": (io.BytesIO(audio.getvalue()), "synthetic.wav"),
        "lang_hint": "es",
        "cleanup_profile": "off",
        "persist_history": "false",
        **fields,
    }
    response = client.post("/v1/stt/transcribe", data=data)
    try:
        return response.status_code, response.get_json()
    finally:
        response.close()


def _recognized(*args, **kwargs):
    return {"text": "hola", "language": "es", "segments": []}


@pytest.mark.parametrize("diarize", [{}, {"diarize": "true"}])
def test_short_deadline_skips_cold_pyannote(client_engine, monkeypatch, diarize):
    client, engine, abort_rest = client_engine
    settings = audio_engine.settings
    monkeypatch.setattr(settings, "DIARIZATION_ENABLED", True)
    monkeypatch.setattr(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True)
    # Также проверить ранний файловый pipeline до обычного STT.
    monkeypatch.setattr(settings, "DIARIZED_TRANSCRIPTION_ENABLED", True)
    monkeypatch.setattr(settings, "DIARIZED_MIN_DURATION_SEC", 0.1)
    monkeypatch.setattr(rest, "_DEADLINE_SEC_MIN", 1.5)
    monkeypatch.setattr(engine, "_transcribe_with_fallback", _recognized)
    release = threading.Event()

    def cold_load():
        release.wait(5.0)
        raise RuntimeError("synthetic cold pipeline")

    monkeypatch.setattr(engine, "_load_diarization_pipeline", cold_load)
    try:
        status, result = post(client, deadline_sec="1.5", **diarize)
    finally:
        release.set()
        assert rest._STT_SINGLEFLIGHT.acquire(timeout=4.0)
        rest.release_stt_singleflight()
    assert status == 200
    assert result["text"] == "hola"
    assert result["diarization"]["skipped"] == "request_deadline"
    abort_rest.assert_not_called()


@pytest.mark.parametrize("deadline", [{}, {"deadline_sec": "26"}])
def test_regular_file_request_keeps_explicit_diarization(client_engine, monkeypatch, deadline):
    client, engine, _abort_rest = client_engine
    monkeypatch.setattr(audio_engine.settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", False)
    monkeypatch.setattr(engine, "_transcribe_with_fallback", _recognized)
    segment = SimpleNamespace(start=0.0, end=1.0)
    annotation = SimpleNamespace(itertracks=lambda *, yield_label: [(segment, None, "SPEAKER_00")])
    monkeypatch.setattr(engine, "_load_diarization_pipeline", lambda: lambda _path: annotation)

    status, result = post(client, diarize="true", **deadline)
    assert status == 200
    assert result["diarization"]["enabled"] is True
    assert result["diarization"]["speaker_segments"][0]["speaker"] == "SPEAKER_00"


def test_short_worker_timeout_does_not_create_a_series_of_503(client_engine, monkeypatch):
    client, _engine, abort_rest = client_engine
    calls = []

    def run_worker(_audio, _params, *, timeout_sec, model_name):
        calls.append(timeout_sec)
        if len(calls) == 1:
            raise MLXTimeoutError(timeout_sec, model_name)
        return _recognized()

    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", run_worker)
    fields = {"deadline_sec": "25", "diarize": "false"}
    statuses = [post(client, **fields)[0] for _ in range(3)]
    assert statuses[0] in {503, 504}
    assert statuses[1:] == [200, 200]
    abort_rest.assert_not_called()


def test_rest_reserves_response_margin_inside_client_deadline(client_engine, monkeypatch):
    client, engine, _abort_rest = client_engine
    remaining = []

    def transcribe(*args, **kwargs):
        remaining.append(stt_budget.remaining_sec())
        return _recognized()

    monkeypatch.setattr(engine, "_transcribe_with_fallback", transcribe)
    assert post(client, deadline_sec="25", diarize="false")[0] == 200
    assert 0.0 < remaining[0] <= 23.0


def test_full_worker_timeout_preserves_model_cooldown(client_engine, monkeypatch):
    client, _engine, _abort_rest = client_engine
    monkeypatch.setattr(audio_engine.settings, "MLX_TRANSCRIBE_TIMEOUT_SEC", 7.0)

    def fail_worker(_audio, _params, *, timeout_sec, model_name):
        raise MLXTimeoutError(timeout_sec, model_name)

    worker = Mock(side_effect=fail_worker)
    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", worker)
    fields = {"deadline_sec": "25", "diarize": "false"}
    assert [post(client, **fields)[0] for _ in range(2)] == [503, 503]
    worker.assert_called_once()


def test_timed_out_whisper_does_not_enter_graceful_shutdown(monkeypatch):
    session = whisper.MLXWhisperSession()
    child = Mock()
    child.stdin.closed = False
    child.stdout.readline.return_value = ""
    child.poll.return_value = -9
    child.wait.return_value = -9
    session._proc = child

    def timer(_delay, kill):
        return Mock(start=kill)

    monkeypatch.setattr(whisper.threading, "Timer", timer)
    with pytest.raises(MLXTimeoutError):
        session._send({"op": "transcribe"}, timeout_sec=1.0, model_name="synthetic-whisper")
    child.kill.assert_called_once()
    assert child.stdin.write.call_count == 1
    assert session._proc is None


def test_hung_subprocess_is_reaped_before_next_whisper_request(monkeypatch, tmp_path):
    worker_script = tmp_path / "synthetic_worker.py"
    worker_script.write_text(
        "import json, sys, threading\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if request['op'] == 'shutdown':\n"
        "        break\n"
        "    if request['params'].get('hang'):\n"
        "        threading.Event().wait()\n"
        "    print(json.dumps({'ok': True, 'result': {'text': 'hola'}}), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(whisper, "_WORKER_SCRIPT", worker_script)
    session = whisper.MLXWhisperSession()
    try:
        session.start()
        original = session._proc
        with pytest.raises(MLXTimeoutError):
            session.transcribe("synthetic.wav", {"hang": True}, 0.5, "synthetic")
        assert original.poll() == -signal.SIGKILL
        assert session.transcribe("synthetic.wav", {}, 5.0, "synthetic") == {"text": "hola"}
        assert session._proc is not original
    finally:
        session.close()
