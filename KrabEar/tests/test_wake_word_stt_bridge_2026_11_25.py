"""Мост STT-споттера к уже загруженному GigaAM-воркеру: без второй копии модели, без ожидания, с дедлайном."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import numpy as np
import pytest

from backend import wake_word_stt_bridge as bridge
from backend.stt_keyword_spotter import TranscriberUnavailable
from core.config import DEFAULT_SETTINGS

SRC = Path(bridge.__file__).read_text(encoding="utf-8")


class _Lease:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.released, self.run_args = result, exc, 0, None

    def run(self, audio, rate, deadline):
        self.run_args = (audio, rate, deadline)
        if self.exc:
            raise self.exc
        return self.result

    def release(self):
        self.released += 1


class _Router:
    def __init__(self, lease=None, status="ok", boom=False):
        self.lease, self.status, self.boom, self.reserved = lease, status, boom, 0

    def reserve_gigaam_call(self):
        self.reserved += 1
        if self.boom:
            raise RuntimeError("router closing")
        return self.lease, self.status


def _call(router, samples=None):
    fn = bridge.make_router_transcriber(lambda: router)
    return fn(np.ones(2000, dtype=np.int16) * 1000 if samples is None else samples)


def test_ok_result_returns_text_and_releases_lease():
    lease = _Lease({"status": "ok", "text": "Краб."})
    assert _call(_Router(lease)) == "Краб."
    assert lease.released == 1
    audio, rate, deadline = lease.run_args
    assert audio.dtype == np.float32 and audio.ndim == 1 and float(np.abs(audio).max()) <= 1.0
    assert rate == 16000
    # дедлайн ограничивает ожидание воркера: диктовка не ждёт дольше ~1.5 с
    assert 0 < deadline - time.monotonic() <= bridge.DEFAULT_DEADLINE_SEC + 0.1


@pytest.mark.parametrize("status", ["busy", "not_ready", "closing"])
def test_busy_or_not_loaded_worker_is_skipped_not_waited(status):
    router = _Router(lease=None, status=status)
    with pytest.raises(TranscriberUnavailable):
        _call(router)
    assert router.reserved == 1


def test_missing_router_or_failing_reserve_is_unavailable():
    with pytest.raises(TranscriberUnavailable):
        _call(None)
    with pytest.raises(TranscriberUnavailable):
        _call(_Router(boom=True))


@pytest.mark.parametrize("result", [None, "text", {"status": "timeout", "text": ""}, {"status": "ok", "text": 5},
                                    {"status": "error", "text": "краб"}])
def test_non_ok_results_never_leak_text_and_lease_is_released(result):
    lease = _Lease(result)
    with pytest.raises(TranscriberUnavailable):
        _call(_Router(lease))
    assert lease.released == 1


def test_run_exception_does_not_leak_its_message_and_releases_lease():
    lease = _Lease(exc=RuntimeError("секретный фрагмент текста краб"))
    with pytest.raises(TranscriberUnavailable) as info:
        _call(_Router(lease))
    assert lease.released == 1
    assert "краб" not in str(info.value) and info.value.__cause__ is None


def test_router_is_resolved_on_every_utterance():
    holder = {"router": None}
    fn = bridge.make_router_transcriber(lambda: holder["router"])
    with pytest.raises(TranscriberUnavailable):
        fn(np.ones(100, dtype=np.int16))
    holder["router"] = _Router(_Lease({"status": "ok", "text": "краб"}))
    assert fn(np.ones(100, dtype=np.int16)) == "краб"


def test_bridge_never_creates_a_model_and_never_persists_or_logs_text():
    tree = ast.parse(SRC)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"gigaam_mlx", "mlx", "mlx_whisper", "socket", "wave", "sqlite3", "pathlib"}
    names = {getattr(n.func, "attr", None) or getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not names & {"open", "write_text", "write_bytes", "GigaAMMLXAdapter", "GigaAMAdapter"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in {"debug", "info", "warning", "error", "exception"}:
            assert "text" not in ast.dump(n)


def test_settings_defaults_keep_old_behaviour_and_validator_rejects_garbage():
    from backend.settings_validator import SettingsValidator

    assert DEFAULT_SETTINGS["wake_word_oww_mode"] == "live"
    assert DEFAULT_SETTINGS["wake_word_stt_mode"] == "off"
    fixed = SettingsValidator().validate({"wake_word_oww_mode": "turbo", "wake_word_stt_mode": "x"}).fixed
    assert fixed["wake_word_oww_mode"] == "live" and fixed["wake_word_stt_mode"] == "off"
    ok = SettingsValidator().validate({"wake_word_oww_mode": "shadow", "wake_word_stt_mode": "live"}).fixed
    assert ok["wake_word_oww_mode"] == "shadow" and ok["wake_word_stt_mode"] == "live"
