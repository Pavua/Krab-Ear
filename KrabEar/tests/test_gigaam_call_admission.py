"""Телефонный lease делит ownership с диктовкой и выгрузкой, без ML-моделей."""
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from core.pipeline.stt_gigaam import GigaAMAdapter
from core.stt_router import STTRouter


def _loaded():
    adapter = GigaAMAdapter(transport="subprocess", mode="v3_e2e_rnnt")
    adapter._active_transport = "subprocess"
    session = Mock()
    session.is_loaded.return_value = True
    session.transcribe.return_value = {"text": "запись", "engine": "gigaam-rnnt"}
    adapter._subprocess = session
    return adapter, session


def test_cold_adapter_never_loads_for_call():
    adapter = GigaAMAdapter(transport="subprocess")
    adapter._get_subprocess_session = Mock(side_effect=AssertionError("cold load"))
    lease, status = adapter.reserve_call()
    assert lease is None and status == "not_ready"
    adapter._get_subprocess_session.assert_not_called()


def test_call_reservation_pins_worker_until_release():
    adapter, session = _loaded()
    lease, status = adapter.reserve_call()
    assert status == "ok" and lease is not None
    assert adapter.inflight == 1
    assert adapter.reserve_call() == (None, "busy")
    assert adapter.close_if_idle(0) is False
    assert adapter.close() is False
    session.close.assert_not_called()
    lease.release()
    lease.release()
    assert adapter.inflight == 0
    assert adapter.close() is True
    session.close.assert_called_once()


def test_dictation_inflight_blocks_call_without_queue():
    adapter, session = _loaded()
    entered, finish = threading.Event(), threading.Event()
    def transcribe(*args, **kwargs):
        entered.set()
        assert finish.wait(2)
        return "готово", "gigaam-rnnt"
    adapter._transcribe_subprocess = transcribe
    thread = threading.Thread(target=adapter.transcribe, args=(np.ones(1000),))
    thread.start()
    try:
        assert entered.wait(2)
        assert adapter.reserve_call() == (None, "busy")
        session.transcribe.assert_not_called()
    finally:
        finish.set()
        thread.join(2)
        adapter.close()
    assert not thread.is_alive()


def test_lease_uses_pinned_session_and_resamples_only_once():
    adapter, session = _loaded()
    adapter._get_subprocess_session = Mock(side_effect=AssertionError("lazy lookup"))
    paths = []
    def transcribe(path, **kwargs):
        import wave
        paths.append(Path(path))
        with wave.open(path) as wav:
            assert wav.getframerate() == 16000
            assert wav.getnframes() == 16000
        assert kwargs["wait_for_slot"] is False
        return {"text": "запись", "engine": "gigaam-rnnt"}
    session.transcribe.side_effect = transcribe
    lease, _ = adapter.reserve_call()
    try:
        result = lease.run(np.ones(8000, dtype=np.float32), 8000, time.monotonic()+5)
        assert result["text"] == "запись"
        assert result["mode"] == "v3_e2e_rnnt"
        assert result["transport"] == "subprocess"
        assert result["confidence_source"] == "constant"
        assert not any(p.exists() for p in paths)
    finally:
        lease.release()
        adapter.close()


def test_router_reservation_never_lazy_creates_and_preserves_busy_cache():
    settings = SimpleNamespace(
        STT_GIGAAM_ENABLED=True, STT_GIGAAM_MODE="v3_e2e_rnnt",
        STT_GIGAAM_DEVICE="mps", STT_GIGAAM_TRANSPORT="subprocess",
    )
    router = STTRouter(settings)
    router.get_gigaam_adapter = Mock(side_effect=AssertionError("lazy adapter"))
    assert router.reserve_gigaam_call() == (None, "not_ready")
    adapter, session = _loaded()
    router._gigaam_adapter = adapter
    router._gigaam_adapter_fingerprint = ("v3_e2e_rnnt", "mps", "subprocess", None)
    lease, status = router.reserve_gigaam_call()
    assert status == "ok"
    assert router.close() is False
    assert router._gigaam_adapter is adapter
    session.close.assert_not_called()
    lease.release()
    assert router.close() is True
    assert router._gigaam_adapter is None


def test_hot_config_change_does_not_replace_a_pinned_worker():
    settings = SimpleNamespace(
        STT_GIGAAM_ENABLED=True, STT_GIGAAM_MODE="v3_e2e_rnnt",
        STT_GIGAAM_DEVICE="mps", STT_GIGAAM_TRANSPORT="subprocess",
    )
    router = STTRouter(settings)
    adapter, session = _loaded()
    router._gigaam_adapter = adapter
    router._gigaam_adapter_fingerprint = ("v3_e2e_rnnt", "mps", "subprocess", None)
    lease, _ = router.reserve_gigaam_call()
    settings.STT_GIGAAM_MODE = "ctc"
    assert router.reserve_gigaam_call() == (None, "not_ready")
    assert router.get_gigaam_adapter() is None
    assert router._gigaam_adapter is adapter
    session.close.assert_not_called()
    lease.release()
    router.close()


def test_session_send_is_nonblocking_and_deadline_does_not_kill_owner():
    import io
    import pytest
    from core.pipeline.stt_gigaam import _GigaAMSubprocessSession, _GigaAMCallBusy
    session = _GigaAMSubprocessSession("unused", "unused", "rnnt", "cpu")
    session._proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO('{"ok":true}\n'))
    session._timeout_kill = Mock(side_effect=AssertionError("phone deadline kills worker"))
    session._lock.acquire()
    try:
        with pytest.raises(_GigaAMCallBusy):
            session._send({"op": "transcribe"}, 120, wait_for_slot=False)
        assert session._proc.stdin.getvalue() == ""
    finally:
        session._lock.release()
    with pytest.raises(TimeoutError):
        session._send({"op": "transcribe"}, 120, wait_for_slot=False,
                      deadline_monotonic=time.monotonic()-1)
    assert session._proc.stdin.getvalue() == ""
    session._timeout_kill.assert_not_called()
    session._proc = None


def test_premature_release_cannot_unpin_running_inference():
    adapter, session = _loaded()
    entered, finish = threading.Event(), threading.Event()
    def transcribe(*args, **kwargs):
        entered.set()
        assert finish.wait(2)
        return {"text": "готово"}
    session.transcribe.side_effect = transcribe
    lease, _ = adapter.reserve_call()
    thread = threading.Thread(
        target=lease.run, args=(np.ones(8000), 8000, time.monotonic()+5),
    )
    thread.start()
    try:
        assert entered.wait(2)
        lease.release()
        assert adapter.inflight == 1
        assert adapter.close() is False
    finally:
        finish.set()
        thread.join(2)
        lease.release()
        adapter.close()
    assert adapter.inflight == 0
