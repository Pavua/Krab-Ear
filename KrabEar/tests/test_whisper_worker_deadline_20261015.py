"""Subprocess Whisper не получает новый бюджет после ожидания или TypeError.

Настоящий AudioEngine, подменены только clock, flock и граница Metal worker.
Ни модель, ни production flock, ни аудиоустройства не используются.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import engine as module
from core import mlx_whisper_session as session
from core.mlx_lock import MLXLockTimeoutError


@pytest.fixture
def worker_attempt(monkeypatch):
    now = [100.0]
    calls = []
    flock_timeouts = []
    state = SimpleNamespace(wait=0.0, error=None, advance=0.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module, "settings", SimpleNamespace(
        TRANSCRIBE_LANGUAGE="ru", MLX_TRANSCRIBE_TIMEOUT_SEC=45.0,
        MLX_CRASH_RECOVERY_ENABLED=True,
    ))
    monkeypatch.setattr(session, "mlx_whisper_worker_enabled", lambda: True)

    @contextmanager
    def flock(timeout_sec=5.0):
        flock_timeouts.append(timeout_sec)
        now[0] += state.wait
        yield

    def transcribe(audio, params, timeout_sec, model_name):
        calls.append((params["language"], timeout_sec))
        now[0] += state.advance
        if state.error is not None:
            raise state.error
        return {"text": "синтетический результат"}

    monkeypatch.setattr(module, "mlx_inter_process_lock", flock)
    monkeypatch.setattr(session, "transcribe_via_mlx_worker", transcribe)
    engine = module.AudioEngine.__new__(module.AudioEngine)
    engine._push_error = lambda *args, **kwargs: None
    return engine, state, calls, flock_timeouts


def invoke(engine, deadline=101.0, language="ru"):
    return engine._transcribe_model(
        "synthetic.wav", "synthetic-model", "", language,
        attempt_timeout_sec=1.0, attempt_deadline_monotonic=deadline,
    )


def test_expired_worker_attempt_never_enters_flock(worker_attempt):
    engine, _state, calls, flocks = worker_attempt
    with pytest.raises(MLXLockTimeoutError):
        invoke(engine, deadline=99.0)
    assert calls == []
    assert flocks == []


def test_expiry_during_flock_never_starts_worker(worker_attempt):
    engine, state, calls, _flocks = worker_attempt
    state.wait = 1.1
    with pytest.raises(MLXLockTimeoutError):
        invoke(engine)
    assert calls == []


def test_compatibility_retry_cannot_restart_expired_budget(worker_attempt):
    engine, state, calls, _flocks = worker_attempt
    state.error = TypeError("unsupported synthetic kwarg")
    state.advance = 1.1
    with pytest.raises(MLXLockTimeoutError):
        invoke(engine)
    assert len(calls) == 1


def test_flock_wait_reduces_worker_watchdog_and_preserves_auto(worker_attempt):
    engine, state, calls, flocks = worker_attempt
    state.wait = 0.75
    assert invoke(engine, language="auto") == {"text": "синтетический результат"}
    assert flocks == [1.0]
    assert len(calls) == 1
    assert calls[0][0] is None
    # 0.25 seconds remain; 80% = 0.20, below the outer attempt deadline.
    assert calls[0][1] == pytest.approx(0.2)


@pytest.mark.parametrize("failure", [MemoryError("allocation"), RuntimeError("GPU failed")])
def test_deadline_preserves_confirmed_inference_failure(worker_attempt, failure):
    engine, state, calls, _flocks = worker_attempt
    state.error = failure
    state.advance = 1.1
    with pytest.raises(type(failure)) as caught:
        invoke(engine)
    assert caught.value is failure
    assert len(calls) == 1


def test_retry_pcm_deadline_preserves_real_worker_error(monkeypatch):
    """Настоящие wrapper/session: второй WAV истекает после RuntimeError child."""
    now = [100.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(session, "time", clock)
    monkeypatch.setattr(module, "settings", SimpleNamespace(
        TRANSCRIBE_LANGUAGE="ru", MLX_TRANSCRIBE_TIMEOUT_SEC=45.0,
        MLX_CRASH_RECOVERY_ENABLED=True,
    ))
    monkeypatch.setattr(session, "mlx_whisper_worker_enabled", lambda: True)
    monkeypatch.setenv("KRAB_EAR_MLX_INTER_PROCESS_LOCK", "0")
    engine = module.AudioEngine.__new__(module.AudioEngine)
    engine._push_error = lambda *args, **kwargs: None
    real_session = session.MLXWhisperSession()
    proc = Mock()
    proc.poll.return_value = None
    proc.stdin.closed = False
    real_session._proc = proc
    monkeypatch.setattr(session, "get_mlx_whisper_session", lambda: real_session)
    writes = []

    def write_pcm(_path, _audio):
        writes.append(True)
        if len(writes) == 2:
            now[0] += 1.0

    def response():
        now[0] += 0.3
        return '{"ok": false, "error": "RuntimeError: confirmed GPU failure"}\n'

    monkeypatch.setattr(session, "_write_pcm_wav", write_pcm)
    proc.stdout.readline.side_effect = response
    try:
        with pytest.raises(RuntimeError, match="confirmed GPU failure"):
            engine._transcribe_model(
                object(), "synthetic-model", "", "ru",
                attempt_timeout_sec=1.0, attempt_deadline_monotonic=101.0,
            )
        assert len(writes) == 2
        assert proc.stdout.readline.call_count == 1
        assert real_session.inflight == 0
    finally:
        real_session.close()
