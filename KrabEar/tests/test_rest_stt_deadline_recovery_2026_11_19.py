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
    store.call_privacy_mode.return_value = False
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
    now = [100.0]
    monkeypatch.setattr(rest.time, "monotonic", lambda: now[0])

    def run_worker(_audio, _params, *, timeout_sec, model_name):
        calls.append(timeout_sec)
        if len(calls) == 1:
            now[0] += timeout_sec
            raise MLXTimeoutError(timeout_sec, model_name)
        return _recognized()

    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", run_worker)
    fields = {"deadline_sec": "25", "diarize": "false"}
    statuses = [post(client, **fields)[0] for _ in range(3)]
    assert statuses[0] == 504
    assert statuses[1:] == [200, 200]
    abort_rest.assert_not_called()


def test_profile_capped_whisper_keeps_remote_fallback_with_time_left(client_engine, monkeypatch):
    client, engine, abort_rest = client_engine
    now = [100.0]
    monkeypatch.setattr(rest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(audio_engine.settings, "NETWORK_MODE", "hybrid")
    store = client.application.config["REST_DEPS"].store
    store.load_settings.return_value = {"stt_timeout_interactive_max_sec": 30.0}
    monkeypatch.setattr(engine, "_remote_stt_retry_configured", lambda: True)
    remote = Mock(side_effect=_recognized)
    monkeypatch.setattr(engine, "_transcribe_remote", remote)

    def worker(_audio, _params, *, timeout_sec, model_name):
        assert timeout_sec == 30.0
        now[0] += timeout_sec
        raise MLXTimeoutError(timeout_sec, model_name)

    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", worker)
    status, body = post(client, deadline_sec="120", diarize="false")
    assert status == 200
    assert body["text"] == "hola"
    remote.assert_called_once()
    abort_rest.assert_not_called()


def test_ru_finetune_timeout_preserves_request_deadline_outcome(client_engine, monkeypatch):
    client, engine, abort_rest = client_engine
    now = [100.0]
    monkeypatch.setattr(rest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(audio_engine.settings, "STT_USE_RU_FINETUNE", True)
    monkeypatch.setattr(audio_engine.settings, "STT_RU_FINETUNE_MODEL", "synthetic-ru-whisper")
    monkeypatch.setattr(audio_engine.settings, "NETWORK_MODE", "hybrid")
    monkeypatch.setattr(engine, "_remote_stt_retry_configured", lambda: True)
    remote = Mock(side_effect=AssertionError("request budget is spent"))
    monkeypatch.setattr(engine, "_transcribe_remote", remote)

    def worker(_audio, _params, *, timeout_sec, model_name):
        assert model_name == "synthetic-ru-whisper"
        now[0] += timeout_sec
        raise MLXTimeoutError(timeout_sec, model_name)

    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", worker)
    status, body = post(client, deadline_sec="25", diarize="false", lang_hint="ru")
    assert status == 504
    assert body == {"error": "Transcription timeout", "text": ""}
    remote.assert_not_called()
    abort_rest.assert_not_called()


def test_explicit_deadline_shares_one_cleanup_reserve_with_whisper(client_engine, monkeypatch):
    client, _engine, abort_rest = client_engine
    now = [100.0]
    monkeypatch.setattr(rest.time, "monotonic", lambda: now[0])

    def worker(_audio, _params, *, timeout_sec, model_name):
        if timeout_sec <= 22.0:
            raise MLXTimeoutError(timeout_sec, model_name)
        now[0] += 22.0
        return _recognized()

    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", worker)
    status, body = post(client, deadline_sec="25", diarize="false")
    assert status == 200
    assert body["text"] == "hola"
    abort_rest.assert_not_called()


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("queue_wait", [18.01, 19.0, 21.0])
def test_queued_request_with_no_useful_budget_skips_whisper(client_engine, monkeypatch, chunked, queue_wait):
    client, _engine, abort_rest = client_engine
    monkeypatch.setattr(audio_engine.settings, "STT_STREAMING_ENABLED", chunked)
    monkeypatch.setattr(audio_engine.settings, "STT_STREAMING_MIN_AUDIO_SEC", 0.5)
    now = [100.0]
    monkeypatch.setattr(rest.time, "monotonic", lambda: now[0])
    acquire = rest.try_acquire_stt_singleflight

    def wait_for_slot(timeout):
        now[0] += queue_wait
        return acquire(timeout)

    monkeypatch.setattr(rest, "try_acquire_stt_singleflight", wait_for_slot)
    worker = Mock(side_effect=AssertionError("no budget for worker"))
    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", worker)
    status, body = post(client, deadline_sec="25", diarize="false")
    assert status == 504
    assert body == {"error": "Transcription timeout", "text": ""}
    worker.assert_not_called()
    abort_rest.assert_not_called()
    assert rest.try_acquire_stt_singleflight(0.0)
    rest.release_stt_singleflight()


@pytest.mark.parametrize("deadline", [10, 25])
def test_rest_reserves_response_margin_inside_client_deadline(client_engine, monkeypatch, deadline):
    client, engine, _abort_rest = client_engine
    remaining = []

    def transcribe(*args, **kwargs):
        remaining.append(stt_budget.remaining_sec())
        return _recognized()

    monkeypatch.setattr(engine, "_transcribe_with_fallback", transcribe)
    assert post(client, deadline_sec=str(deadline), diarize="false")[0] == 200
    assert 0.0 < remaining[0] <= deadline - 2.0


def test_explicit_deadline_counts_preprocessing_and_waiting_for_slot(client_engine, monkeypatch):
    client, engine, _abort_rest = client_engine
    clock = [100.0]
    monkeypatch.setattr(rest.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engine, "normalize_audio", lambda _path: clock.__setitem__(0, 104.0))
    wait_budget, work_budget = [], []
    acquire = rest.try_acquire_stt_singleflight

    def acquire_after_wait(timeout):
        wait_budget.append(timeout)
        clock[0] += 13.99
        return acquire(0.0)

    def transcribe(_audio, _params, *, timeout_sec, model_name):
        work_budget.append(timeout_sec)
        return _recognized()

    monkeypatch.setattr(rest, "try_acquire_stt_singleflight", acquire_after_wait)
    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", transcribe)
    assert post(client, deadline_sec="25", diarize="false")[0] == 200
    assert wait_budget == [21.0]
    assert work_budget == pytest.approx([5.01])


def test_exhaustion_before_stt_admission_does_not_start_work(client_engine, monkeypatch):
    client, engine, abort_rest = client_engine
    clock = [100.0]
    monkeypatch.setattr(rest.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engine, "normalize_audio", lambda _path: clock.__setitem__(0, 126.0))
    transcribe = Mock(return_value=_recognized())
    monkeypatch.setattr(engine, "_transcribe_with_fallback", transcribe)

    assert post(client, deadline_sec="25", diarize="false")[0] == 504
    transcribe.assert_not_called()
    abort_rest.assert_not_called()


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


def test_short_timeout_keeps_earlier_confirmed_worker_failure(client_engine, monkeypatch):
    client, _engine, _abort_rest = client_engine
    worker_calls = []

    def worker(_audio, _params, *, timeout_sec, model_name):
        worker_calls.append(timeout_sec)
        if len(worker_calls) == 1:
            raise RuntimeError("synthetic GPU failure")
        if len(worker_calls) == 2:
            raise MLXTimeoutError(timeout_sec, model_name)
        return _recognized()

    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", worker)
    fields = {"deadline_sec": "25", "diarize": "false"}
    assert [post(client, **fields)[0] for _ in range(2)] == [503, 503]
    assert len(worker_calls) == 2


@pytest.mark.parametrize(
    ("fields", "expect_speakers"),
    [({"deadline_sec": "25"}, False), ({"request_profile": "voice_gateway_call"}, False), ({}, True)],
)
def test_rest_diarization_policy_reaches_whisperx_fallback(client_engine, monkeypatch, fields, expect_speakers):
    client, engine, _abort_rest = client_engine
    settings = audio_engine.settings
    monkeypatch.setattr(settings, "WHISPERX_ENABLED", True)
    monkeypatch.setattr(settings, "WHISPERX_DIARIZATION", True)
    monkeypatch.setattr(settings, "WHISPERX_WORD_TIMESTAMPS", False)
    monkeypatch.setattr(settings, "HF_TOKEN", "synthetic-fixture")
    monkeypatch.setattr(settings, "DIARIZATION_ENABLED", False)
    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", Mock(side_effect=RuntimeError("synthetic failure")))
    model = SimpleNamespace(
        transcribe=lambda *_args, **_kwargs: {"segments": [{"text": "hola"}], "language": "es"},
    )
    monkeypatch.setattr(engine, "_load_whisperx_model", lambda: model)
    load_speakers = Mock(return_value=lambda _audio: [])
    monkeypatch.setattr(audio_engine, "_whisperx", SimpleNamespace(
        load_audio=lambda _path: object(),
        DiarizationPipeline=load_speakers,
    ))

    status, result = post(client, **fields)
    assert status == 200
    assert result["text"] == "hola"
    assert result["engine"] == "whisperx"
    assert load_speakers.called == expect_speakers


def test_call_with_six_seconds_can_still_start_whisper(client_engine, monkeypatch):
    client, _engine, abort_rest = client_engine
    monkeypatch.setattr(rest.time, "monotonic", lambda: 100.0)
    budgets = []

    def worker(_audio, _params, *, timeout_sec, model_name):
        budgets.append(timeout_sec)
        return _recognized()

    monkeypatch.setattr(whisper, "transcribe_via_mlx_worker", worker)
    status, body = post(client, request_profile="voice_gateway_call", deadline_sec="6")
    assert status == 200
    assert body["text"] == "hola"
    assert budgets == pytest.approx([4.64])
    abort_rest.assert_not_called()


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
