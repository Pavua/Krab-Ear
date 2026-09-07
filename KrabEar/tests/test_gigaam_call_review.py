"""Независимые проверки ownership телефонного GigaAM без ML-процессов."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import wave

import numpy as np
import pytest

from backend.call_stt_service import CallSTTService
from core.pipeline.stt_gigaam import GigaAMAdapter, _GigaAMSubprocessSession
from core.stt_router import STTRouter


def _ready_router():
    settings = SimpleNamespace(
        STT_GIGAAM_ENABLED=True, STT_GIGAAM_MODE="v3_e2e_rnnt",
        STT_GIGAAM_DEVICE="mps", STT_GIGAAM_TRANSPORT="subprocess",
        STT_GIGAAM_VENV_PYTHON="",
    )
    adapter = GigaAMAdapter(mode="v3_e2e_rnnt", transport="subprocess")
    session = Mock()
    session.is_loaded.return_value = True
    adapter._active_transport = "subprocess"
    adapter._subprocess = session
    router = STTRouter(settings)
    router._gigaam_adapter = adapter
    router._gigaam_adapter_fingerprint = ("v3_e2e_rnnt", "mps", "subprocess", None)
    return router, adapter, session


def test_invalid_new_python_override_cannot_admit_previous_default_worker():
    router, adapter, session = _ready_router()
    router._settings.STT_GIGAAM_VENV_PYTHON = "/tmp/untrusted/not-python"
    lease, status = router.reserve_gigaam_call()
    try:
        assert lease is None and status == "not_ready"
        assert adapter.inflight == 0
    finally:
        if lease:
            lease.release()
        router.close()
    session.transcribe.assert_not_called()


def test_failed_worker_close_does_not_discard_owner_or_report_success():
    router, adapter, session = _ready_router()
    session.close.side_effect = RuntimeError("worker close failed")
    try:
        assert router.close() is False
        assert router._gigaam_adapter is adapter
        assert adapter._subprocess is session
    finally:
        session.close.side_effect = None
        router.close()


def test_close_does_not_wait_for_dictation_cold_load_lock():
    router, adapter, session = _ready_router()
    loading, finish_load, closed = threading.Event(), threading.Event(), threading.Event()
    results = []

    def load():
        # _get_subprocess_session держит этот lock вокруг session.start(),
        # чей обычный предел загрузки — 180 секунд.
        with adapter._spawn_lock:
            adapter.inflight = 1
            loading.set()
            assert finish_load.wait(3)
            adapter.inflight = 0

    def close():
        results.append(router.close())
        closed.set()

    loader = threading.Thread(target=load)
    closer = threading.Thread(target=close)
    loader.start()
    try:
        assert loading.wait(1)
        closer.start()
        promptly_closed = closed.wait(1)
        session.close.assert_not_called()
    finally:
        finish_load.set()
        loader.join(2)
        if closer.ident is not None:
            closer.join(2)
        router.close()
    assert promptly_closed, "shutdown blocked behind unbounded model load lock"
    assert results == [False]


def _params(deadline):
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(np.full(8000, 1000, dtype=np.int16).tobytes())
    return {
        "request_id": "review-call", "language": "ru",
        "deadline_monotonic": deadline,
        "audio_wav_b64": base64.b64encode(data.getvalue()).decode("ascii"),
    }


def test_real_session_late_drain_preserves_temp_and_serializes_dictation():
    router, adapter, _ = _ready_router()
    entered, finish = threading.Event(), threading.Event()
    dictation_entered = threading.Event()
    written = []
    read_count = 0

    class Writer:
        def write(self, line):
            written.append(json.loads(line))

        def flush(self):
            pass

    class Reader:
        def readline(self):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                entered.set()
                assert finish.wait(3)
                return '{"ok":true,"text":"поздний звонок"}\n'
            return '{"ok":true,"text":"моя диктовка"}\n'

    session = _GigaAMSubprocessSession("unused", "unused", "v3_e2e_rnnt", "mps")
    session._proc = SimpleNamespace(stdin=Writer(), stdout=Reader(), poll=lambda: None)
    session._loaded = True
    session._timeout_kill = Mock(side_effect=AssertionError("short deadline killed owner"))
    adapter._subprocess = session
    service = CallSTTService(router, lambda: False, shutdown_timeout_sec=0.01)
    results = []
    thread = None
    timer_patch = patch("core.pipeline.stt_gigaam.threading.Timer", wraps=threading.Timer)
    timer = timer_patch.start()
    try:
        # Настоящий handle/lease/session._send, только pipe I/O герметичен.
        response = service.handle(_params(time.monotonic() + 0.2))
        assert entered.is_set()
        assert response == {"status": "timeout", "text": ""}
        assert adapter.inflight == 1
        call_path = Path(written[0]["audio_path"])
        assert call_path.exists()
        assert service.handle(_params(time.monotonic() + 1))["status"] == "busy"
        assert router.close() is False
        assert adapter.close_if_idle(0) is False

        original = adapter._mark_inflight_start
        def mark_dictation():
            original()
            dictation_entered.set()
        adapter._mark_inflight_start = mark_dictation
        thread = threading.Thread(target=lambda: results.append(adapter.transcribe(np.ones(16000))))
        thread.start()
        assert dictation_entered.wait(1)
        # Звонок ещё читает свою строку; диктовка не может перехватить ответ.
        assert len(written) == 1
        assert service.close() is False
        assert call_path.exists()
        finish.set()
        thread.join(2)
        assert not thread.is_alive()
        assert results[0]["text"] == "моя диктовка"
        assert service.close(timeout_sec=1) is True
        assert not call_path.exists()
        assert adapter.inflight == 0
        session._timeout_kill.assert_not_called()
        assert len(timer.call_args_list) == 2
        assert all(call.args[0] == 120.0 for call in timer.call_args_list)
    finally:
        finish.set()
        if thread is not None:
            thread.join(3)
        service.close(timeout_sec=1)
        session._proc = None
        adapter.close()
        timer_patch.stop()


def test_dead_session_lookup_does_not_close_a_pinned_call_session():
    router, adapter, session = _ready_router()
    lease, status = router.reserve_gigaam_call()
    assert status == "ok"
    session.is_loaded.return_value = False
    replacement = Mock()
    with patch("core.pipeline.stt_gigaam._GigaAMSubprocessSession", return_value=replacement):
        try:
            # Диктовка обнаруживает смерть worker после call admission,
            # но до завершения run/late drain держателя lease.
            try:
                adapter._get_subprocess_session()
            except RuntimeError:
                pass
            session.diagnose_and_close.assert_not_called()
            replacement.start.assert_not_called()
            assert adapter._subprocess is session
        finally:
            lease.release()
            adapter._subprocess = session
            session.is_loaded.return_value = True
            adapter.close()


@pytest.mark.parametrize("corrupt_settings", ["{broken", "null", "[]"])
def test_real_backend_privacy_provider_refuses_corrupt_settings(corrupt_settings):
    from test_ipc_dispatch_build import _build_minimal_backend_service

    service = _build_minimal_backend_service()
    router, _adapter, session = _ready_router()
    session.transcribe.return_value = {"text": "приватный текст звонка"}
    service._call_stt._router = router
    try:
        # Настоящий StateStore + callback, созданный BackendService.__init__.
        service.store.settings_path.write_text(corrupt_settings)
        response = service.handle_request({
            "id": "review", "method": "transcribe_ephemeral_call",
            "params": _params(time.monotonic() + 2),
        })
        assert response["result"] == {"status": "privacy_mode", "text": ""}
        session.transcribe.assert_not_called()
    finally:
        service.close()
        router.close()
