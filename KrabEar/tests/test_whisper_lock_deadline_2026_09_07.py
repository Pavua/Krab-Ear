"""Whisper: истёкшая попытка не должна позже запускать инференс из очереди.

Только настоящий Python RLock и подменённый transcribe. AudioEngine.__init__,
Metal inference, subprocess worker и production lockfile не запускаются.
"""
from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from core import stt_budget
from core.mlx_lock import MLXLockTimeoutError
from core.mlx_inter_lock import MLXInterLockTimeout


class _ObservedRLock:
    """Реальный lock с сигналом начала ожидания: RED не зависит от старта треда."""

    def __init__(self) -> None:
        self.real = threading.RLock()
        self.entered = threading.Event()

    def acquire(self, *args, **kwargs):
        self.entered.set()
        return self.real.acquire(*args, **kwargs)

    def release(self):
        self.real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()
        return False


@pytest.fixture
def isolated_whisper(monkeypatch):
    engine_module = importlib.import_module("core.engine")
    lock_module = importlib.import_module("core.mlx_lock")
    session_module = importlib.import_module("core.mlx_whisper_session")
    infer = Mock(return_value={"text": "тестовый результат"})
    lock = _ObservedRLock()
    monkeypatch.setattr(lock_module, "_mlx_lock", lock)
    monkeypatch.setattr(engine_module, "mlx_whisper", SimpleNamespace(transcribe=infer))
    monkeypatch.setattr(engine_module, "settings", SimpleNamespace(
        TRANSCRIBE_LANGUAGE="ru",
        MLX_CRASH_RECOVERY_ENABLED=False,
        MLX_TRANSCRIBE_TIMEOUT_SEC=60.0,
    ))
    monkeypatch.setattr(session_module, "mlx_whisper_worker_enabled", lambda: False)
    monkeypatch.setenv("KRAB_EAR_MLX_INTER_PROCESS_LOCK", "0")
    engine = engine_module.AudioEngine.__new__(engine_module.AudioEngine)
    engine._unavailable_models = {}
    return engine, infer, lock


def test_attempt_timeout_retires_waiter_without_late_inference(isolated_whisper):
    engine, infer, lock = isolated_whisper
    done = threading.Event()
    errors = []

    def run():
        try:
            engine._transcribe_model(
                np.zeros(1600, dtype=np.float32), "test-whisper", "", "ru",
                attempt_timeout_sec=0.05,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    lock.real.acquire()
    worker = threading.Thread(target=run, name="test-whisper-lock-wait", daemon=True)
    worker.start()
    try:
        assert lock.entered.wait(5), "Рабочий поток не дошёл до lock acquire"
        # Запас 40x: проверяем завершение ожидания, не точность wallclock.
        finished_before_release = done.wait(2)
    finally:
        lock.real.release()
        worker.join(5)

    observed = {
        "finished_before_release": finished_before_release,
        "error_type": type(errors[0]).__name__ if errors else None,
        "late_inference": infer.called,
        "worker_alive": worker.is_alive(),
    }
    assert observed == {
        "finished_before_release": True,
        "error_type": "MLXLockTimeoutError",
        "late_inference": False,
        "worker_alive": False,
    }


def test_expired_direct_request_does_not_enter_inference(isolated_whisper):
    engine, infer, _lock = isolated_whisper
    with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE, deadline_sec=0.0):
        with pytest.raises(MLXLockTimeoutError):
            engine._transcribe_model(
                np.zeros(1600, dtype=np.float32), "test-whisper", "", "ru",
            )
    infer.assert_not_called()


def test_free_lock_still_allows_inference(isolated_whisper):
    engine, infer, lock = isolated_whisper
    result = engine._transcribe_model(
        np.zeros(1600, dtype=np.float32), "test-whisper", "", "ru",
        attempt_timeout_sec=1.0,
    )
    assert result == {"text": "тестовый результат"}
    infer.assert_called_once()
    assert lock.entered.is_set()


def test_queue_timeout_does_not_blacklist_model(isolated_whisper):
    engine, _infer, _lock = isolated_whisper
    exc = MLXLockTimeoutError("тест: GPU занят")
    assert not engine._blacklist_allowed_for(exc)
    assert not engine._blacklist_allowed_for(exc, is_adapter=True)


def test_inter_process_queue_timeout_does_not_blacklist_model(isolated_whisper):
    engine, _infer, _lock = isolated_whisper
    exc = MLXInterLockTimeout(0.05, Path("/tmp/unused-whisper-probe.lock"))
    assert not engine._blacklist_allowed_for(exc)
    assert not engine._blacklist_allowed_for(exc, is_adapter=True)


def test_absolute_deadline_survives_pool_hop(isolated_whisper, monkeypatch):
    import concurrent.futures
    engine, infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    with stt_budget.stt_budget_scope(stt_budget.BATCH, deadline_sec=60.0):
        deadline = module._mlx_attempt_deadline(120.0)
    assert 100.0 < deadline <= 160.0
    now[0] = deadline + 1.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            engine._transcribe_model, np.zeros(1600), "test", "", "ru",
            attempt_timeout_sec=120.0,
            attempt_deadline_monotonic=deadline,
        )
        with pytest.raises(MLXLockTimeoutError):
            future.result(timeout=2)
    infer.assert_not_called()

def test_wait_reduces_inner_watchdog_budget(isolated_whisper, monkeypatch):
    import contextlib
    engine, infer, lock = isolated_whisper
    module = importlib.import_module("core.engine")
    now = [100.0]
    seen = {}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    module.settings.MLX_CRASH_RECOVERY_ENABLED = True
    acquire = Mock(wraps=lock.acquire)
    monkeypatch.setattr(lock, "acquire", acquire)

    @contextlib.contextmanager
    def delayed_flock(**kwargs):
        seen["flock_timeout"] = kwargs["timeout_sec"]
        now[0] = 100.75
        yield

    def watchdog(*, fn, timeout_sec, model_name):
        seen["watchdog_timeout"] = timeout_sec
        return fn()

    monkeypatch.setattr(module, "mlx_inter_process_lock", delayed_flock)
    monkeypatch.setattr(module, "get_watchdog", lambda: SimpleNamespace(run_with_timeout=watchdog))
    engine._transcribe_model(
        np.zeros(1600), "test", "", "ru",
        attempt_timeout_sec=1.0, attempt_deadline_monotonic=101.0,
    )
    assert seen["flock_timeout"] == 1.0
    assert acquire.call_args.kwargs["timeout"] == 0.25
    assert seen["watchdog_timeout"] == pytest.approx(0.2)
    infer.assert_called_once()

def test_variant_does_not_restart_expired_budget(isolated_whisper, monkeypatch):
    engine, infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def unsupported(*_args, **_kwargs):
        now[0] = 102.0
        raise TypeError("unsupported argument")

    infer.side_effect = unsupported
    with pytest.raises(MLXLockTimeoutError):
        engine._transcribe_model(
            np.zeros(1600), "test", "", "ru",
            attempt_timeout_sec=1.0, attempt_deadline_monotonic=101.0,
        )
    infer.assert_called_once()

def test_expired_watchdog_callback_does_not_enter_gpu(isolated_whisper, monkeypatch):
    engine, infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    module.settings.MLX_CRASH_RECOVERY_ENABLED = True

    def late_watchdog(*, fn, timeout_sec, model_name):
        now[0] = 102.0
        return fn()

    monkeypatch.setattr(module, "get_watchdog", lambda: SimpleNamespace(run_with_timeout=late_watchdog))
    with pytest.raises(MLXLockTimeoutError):
        engine._transcribe_model(
            np.zeros(1600), "test", "", "ru",
            attempt_timeout_sec=1.0, attempt_deadline_monotonic=101.0,
        )
    infer.assert_not_called()


def _prepare_chain(engine, module, monkeypatch, *, finetune=False):
    import contextlib

    engine.quality_profile = "balanced"
    engine.current_model = "test-whisper"
    engine._router = None
    values = {
        "STT_USE_RU_FINETUNE": finetune,
        "STT_RU_FINETUNE_MODEL": "test-finetune",
        "STT_GIGAAM_ENABLED": False,
        "PARAKEET_ENABLED": False,
        "SENSEVOICE_ENABLED": False,
        "WHISPERX_ENABLED": False,
        "VOXTRAL_ENABLED": False,
        "PARAKEET_MODEL": "unused",
        "SENSEVOICE_MODEL": "unused",
        "WHISPERX_MODEL": "unused",
        "VOXTRAL_MODEL": "unused",
        "MODEL_BALANCED": "test-whisper",
        "model_max_list": ["test-retry"],
        "NETWORK_MODE": "offline_strict",
        "STT_MIN_CONFIDENCE_THRESHOLD": 0.5,
        "STT_MAX_RETRIES": 1,
    }
    for key, value in values.items():
        setattr(module.settings, key, value)
    monkeypatch.setattr(module, "_profiler", SimpleNamespace(start_span=lambda _name: contextlib.nullcontext()))
    monkeypatch.setattr(module, "should_skip_second_mlx_checkpoint", lambda: False)
    monkeypatch.setattr(engine, "_raw_confidence_from_result", lambda result: result["confidence"])


@pytest.mark.parametrize("route", ["primary", "multipass", "finetune"])
def test_real_pool_call_sites_pass_frozen_request_deadline(isolated_whisper, monkeypatch, route):
    engine, _infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    _prepare_chain(engine, module, monkeypatch, finetune=route == "finetune")
    calls = []

    def capture(audio, model, prompt, language=None, **kwargs):
        calls.append((threading.get_ident(), model, kwargs))
        return {"text": "готово", "confidence": 0.9}

    monkeypatch.setattr(engine, "_transcribe_model", capture)
    started = time.monotonic()
    with stt_budget.stt_budget_scope(stt_budget.BATCH, deadline_sec=7.0) as budget:
        if route == "multipass":
            result = engine._maybe_multipass_retry(
                np.zeros(1600), "", "ru",
                {"text": "первый", "confidence": 0.1, "model_used": "test-whisper"},
            )
        else:
            result = engine._transcribe_with_fallback_impl(np.zeros(1600), "", "ru")
        request_deadline = budget.deadline_monotonic

    assert result["text"] == "готово"
    assert len(calls) == 1
    worker_id, model, kwargs = calls[0]
    assert worker_id != threading.get_ident(), "Нужен настоящий переход в рабочий поток"
    assert model == {"primary": "test-whisper", "multipass": "test-retry", "finetune": "test-finetune"}[route]
    assert 0 < kwargs["attempt_timeout_sec"] <= 7.0
    assert started < kwargs["attempt_deadline_monotonic"] <= request_deadline


@pytest.mark.parametrize("finetune", [False, True])
@pytest.mark.parametrize("queue_kind", ["intra", "inter"])
def test_queue_timeout_does_not_blacklist_real_chain(isolated_whisper, monkeypatch, finetune, queue_kind):
    engine, _infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    _prepare_chain(engine, module, monkeypatch, finetune=finetune)
    calls = []

    def busy(audio, model, prompt, language=None, **kwargs):
        calls.append(model)
        if queue_kind == "intra":
            raise MLXLockTimeoutError("тест: intra-process очередь")
        raise MLXInterLockTimeout(0.05, Path("/tmp/unused-whisper-probe.lock"))

    monkeypatch.setattr(engine, "_transcribe_model", busy)
    with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE, deadline_sec=7.0):
        with pytest.raises(RuntimeError, match="Все доступные STT"):
            engine._transcribe_with_fallback_impl(np.zeros(1600), "", "ru")
    assert calls == (["test-finetune", "test-whisper"] if finetune else ["test-whisper"])
    assert engine._unavailable_models == {}


@pytest.mark.parametrize("error_type", [MemoryError, RuntimeError])
def test_expiry_preserves_real_inference_failure(isolated_whisper, monkeypatch, error_type):
    engine, infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    failure = error_type("inference failed")

    def fail(*_args, **_kwargs):
        now[0] = 102.0
        raise failure

    infer.side_effect = fail
    with pytest.raises(error_type) as caught:
        engine._transcribe_model(
            np.zeros(1600), "test", "", "ru",
            attempt_timeout_sec=1.0, attempt_deadline_monotonic=101.0,
        )
    assert caught.value is failure
    infer.assert_called_once()


def test_later_type_error_does_not_erase_real_failure_at_expiry(isolated_whisper, monkeypatch):
    engine, infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    failure = RuntimeError("engine failure before unsupported variant")

    def fail(*_args, **_kwargs):
        if infer.call_count == 1:
            raise failure
        now[0] = 102.0
        raise TypeError("unsupported argument")

    infer.side_effect = fail
    with pytest.raises(RuntimeError) as caught:
        engine._transcribe_model(
            np.zeros(1600), "test", "", "ru",
            attempt_timeout_sec=1.0, attempt_deadline_monotonic=101.0,
        )
    assert caught.value is failure
    assert infer.call_count == 2


def test_real_chain_still_blacklists_oom_after_deadline(isolated_whisper, monkeypatch):
    engine, infer, _lock = isolated_whisper
    module = importlib.import_module("core.engine")
    _prepare_chain(engine, module, monkeypatch)
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def oom(*_args, **_kwargs):
        now[0] = 108.0
        raise MemoryError("failed to allocate test buffer")

    infer.side_effect = oom
    with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE, deadline_sec=7.0):
        with pytest.raises(RuntimeError, match="Все доступные STT"):
            engine._transcribe_with_fallback_impl(np.zeros(1600), "", "ru")
    infer.assert_called_once()
    assert engine._unavailable_models == {"test-whisper": 108.0}
