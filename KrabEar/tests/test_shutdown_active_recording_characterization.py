"""Изолированная характеристика hard-exit при активном аудиобуфере."""
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from backend.service import _shutdown_backend


def test_ipc_drain_failure_requests_hard_exit_with_active_recording():
    recorder = SimpleNamespace(_is_recording=True, _chunks=[b"synthetic-audio"])
    service = SimpleNamespace(
        _close_guard=threading.Lock(), _rest_gate_unknown=False,
        _rest_watchdog=None, _rest_inprocess=None, _call_stt=None,
        recorder=recorder, close=Mock(),
    )
    server = SimpleNamespace(stop=Mock(return_value=False))
    handler = SimpleNamespace(shutdown=Mock())
    exits = []

    result = _shutdown_backend(service, server, handler, flush_fn=lambda: None, exit_fn=exits.append)

    assert result is False
    assert exits == [os.EX_SOFTWARE]
    assert recorder._is_recording and recorder._chunks == [b"synthetic-audio"]
    service.close.assert_not_called()
    handler.shutdown.assert_not_called()
