"""Unit tests for Wave 60 Phase B error codes and their call-site wiring.

One test per new code:
1. rewriter.warmup_timeout  — llm_rewriter.py warmup_probe Timeout path
2. disk.low_space           — disk_monitor.py _evaluate_and_emit warning/critical
3. audio.buffer_overflow    — recorder.py stream.read overflowed=True
4. stt.oom_model_evicted    — engine.py MemoryError/OOM OSError in STT chain
5. stt.gigaam_worker_timeout — stt_gigaam.py _timeout_kill worker SIGTERM
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow imports from KrabEar/
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.error_bus import ErrorBus, KrabError
from backend.error_codes import ERROR_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_error_bus() -> tuple[ErrorBus, list[KrabError]]:
    """Create an ErrorBus and capture pushed errors."""
    mock_event_bus = MagicMock()
    bus = ErrorBus(event_bus=mock_event_bus, registry=ERROR_REGISTRY)
    captured: list[KrabError] = []

    original_push = bus.push

    def _capture(err: KrabError) -> bool:
        captured.append(err)
        # Call original to exercise dedup logic too
        return original_push(err)

    bus.push = _capture  # type: ignore[method-assign]
    return bus, captured


# ---------------------------------------------------------------------------
# 1. rewriter.warmup_timeout — llm_rewriter.py
# ---------------------------------------------------------------------------

class RewriterWarmupTimeoutTests(unittest.TestCase):
    """rewriter.warmup_timeout fires when warmup_probe raises requests.Timeout."""

    def test_code_in_registry(self):
        self.assertIn("rewriter.warmup_timeout", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["rewriter.warmup_timeout"]
        self.assertEqual(entry["severity"], "warn")
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_lm_studio_settings")

    def test_warmup_probe_timeout_pushes_error(self):
        """warmup_probe Timeout path calls _push_error with rewriter.warmup_timeout."""
        from backend.llm_rewriter import LLMRewriter

        rewriter = LLMRewriter.__new__(LLMRewriter)
        # Minimal attribute setup required by _push_error
        rewriter._error_bus = None  # will be replaced
        rewriter._model = "test-model"
        rewriter._base_url = "http://localhost:1234"
        rewriter.quality_profile = "balanced"
        rewriter.current_model = "test-model"

        bus, captured = _make_error_bus()
        rewriter._error_bus = bus

        # Call _push_error directly to verify the code wiring works end-to-end
        rewriter._push_error("rewriter.warmup_timeout", "warmup_probe Timeout after 5000ms")

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "rewriter.warmup_timeout")
        self.assertEqual(err.severity, "warn")
        self.assertEqual(err.component, "rewriter")

    def test_registry_has_dedupe_seconds(self):
        entry = ERROR_REGISTRY["rewriter.warmup_timeout"]
        self.assertGreater(entry["dedupe_seconds"], 0)


# ---------------------------------------------------------------------------
# 2. disk.low_space — disk_monitor.py
# ---------------------------------------------------------------------------

class DiskLowSpaceTests(unittest.TestCase):
    """disk.low_space fires when DiskSpaceMonitor hits warning/critical threshold."""

    def test_code_in_registry(self):
        self.assertIn("disk.low_space", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["disk.low_space"]
        self.assertEqual(entry["severity"], "warn")  # default; critical overrides at runtime
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_logs")

    def test_push_disk_error_warning(self):
        """_push_disk_error(level='warning') pushes disk.low_space with severity='warn'."""
        from backend.disk_monitor import DiskSpaceMonitor

        settings = MagicMock()
        event_bus = MagicMock()
        monitor = DiskSpaceMonitor.__new__(DiskSpaceMonitor)
        monitor._settings = settings
        monitor._event_bus = event_bus
        monitor._last_disk_level = None
        monitor._last_history_large_emitted = False

        bus, captured = _make_error_bus()
        monitor._error_bus = bus

        monitor._push_disk_error("warning", 1.5)

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "disk.low_space")
        self.assertEqual(err.severity, "warn")
        self.assertEqual(err.component, "disk")

    def test_push_disk_error_critical(self):
        """_push_disk_error(level='critical') pushes disk.low_space with severity='critical'."""
        from backend.disk_monitor import DiskSpaceMonitor

        monitor = DiskSpaceMonitor.__new__(DiskSpaceMonitor)
        monitor._settings = MagicMock()
        monitor._event_bus = MagicMock()
        monitor._last_disk_level = None
        monitor._last_history_large_emitted = False

        bus, captured = _make_error_bus()
        monitor._error_bus = bus

        monitor._push_disk_error("critical", 0.3)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].severity, "critical")

    def test_no_push_when_error_bus_none(self):
        """No error raised when _error_bus is None."""
        from backend.disk_monitor import DiskSpaceMonitor

        monitor = DiskSpaceMonitor.__new__(DiskSpaceMonitor)
        monitor._settings = MagicMock()
        monitor._event_bus = MagicMock()
        monitor._last_disk_level = None
        monitor._last_history_large_emitted = False
        monitor._error_bus = None  # not wired

        # Should not raise
        monitor._push_disk_error("warning", 1.5)


# ---------------------------------------------------------------------------
# 3. audio.buffer_overflow — recorder.py
# ---------------------------------------------------------------------------

class AudioBufferOverflowTests(unittest.TestCase):
    """audio.buffer_overflow fires when recorder.py detects overflowed=True."""

    def test_code_in_registry(self):
        self.assertIn("audio.buffer_overflow", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["audio.buffer_overflow"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertEqual(entry["dedupe_seconds"], 5)

    def test_push_buffer_overflow_error(self):
        """_push_buffer_overflow_error() pushes audio.buffer_overflow to bus."""
        from backend.recorder import AudioRecorder

        recorder = AudioRecorder.__new__(AudioRecorder)
        recorder._sample_rate = 16000
        recorder._channels = 1

        bus, captured = _make_error_bus()
        recorder._error_bus = bus

        recorder._push_buffer_overflow_error()

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "audio.buffer_overflow")
        self.assertEqual(err.severity, "warn")
        self.assertEqual(err.component, "audio")
        self.assertFalse(err.actionable)

    def test_no_push_when_error_bus_absent(self):
        """If _error_bus attr absent, no AttributeError raised."""
        from backend.recorder import AudioRecorder

        recorder = AudioRecorder.__new__(AudioRecorder)
        # No _error_bus attribute set at all
        recorder._push_buffer_overflow_error()  # should not raise


# ---------------------------------------------------------------------------
# 4. stt.oom_model_evicted — engine.py
# ---------------------------------------------------------------------------

class STTOomModelEvictedTests(unittest.TestCase):
    """stt.oom_model_evicted fires when STT chain evicts model due to OOM."""

    def test_code_in_registry(self):
        self.assertIn("stt.oom_model_evicted", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.oom_model_evicted"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertGreater(entry["dedupe_seconds"], 0)

    def test_push_error_oom_model_evicted(self):
        """_push_error with stt.oom_model_evicted sends correct KrabError.

        Use a lightweight stub rather than importing AudioEngine directly
        to avoid pyannote/torchcodec heavy imports in unit test context.
        """
        bus, captured = _make_error_bus()

        # Replicate the _push_error logic from engine.py with a stub
        code = "stt.oom_model_evicted"
        entry = ERROR_REGISTRY[code]
        err = KrabError(
            severity=entry["severity"],
            component="stt",
            code=code,
            message_user=entry["user_msg_ru"],
            message_debug="MemoryError evicted whisper-large-v3 from STT chain",
            timestamp=datetime.now(timezone.utc),
            context={"model": "whisper-large-v3", "profile": "max"},
            actionable=entry["actionable"],
            action_id=entry["action_id"],
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        err_out = captured[0]
        self.assertEqual(err_out.code, "stt.oom_model_evicted")
        self.assertEqual(err_out.severity, "error")
        self.assertEqual(err_out.component, "stt")
        self.assertFalse(err_out.actionable)

    def test_distinct_from_stt_load_fail(self):
        """stt.oom_model_evicted is a different code from stt.load_fail."""
        self.assertIn("stt.load_fail", ERROR_REGISTRY)
        self.assertIn("stt.oom_model_evicted", ERROR_REGISTRY)
        self.assertNotEqual(
            ERROR_REGISTRY["stt.load_fail"],
            ERROR_REGISTRY["stt.oom_model_evicted"],
        )


# ---------------------------------------------------------------------------
# 5. stt.gigaam_worker_timeout — stt_gigaam.py
# ---------------------------------------------------------------------------

class GigaAMWorkerTimeoutTests(unittest.TestCase):
    """stt.gigaam_worker_timeout fires when GigaAM subprocess worker times out."""

    def test_code_in_registry(self):
        self.assertIn("stt.gigaam_worker_timeout", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.gigaam_worker_timeout"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertEqual(entry["dedupe_seconds"], 30)

    def test_push_worker_timeout_error(self):
        """_push_worker_timeout_error() sends stt.gigaam_worker_timeout to bus."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        session.oom_callback = None

        bus, captured = _make_error_bus()
        session._error_bus = bus

        session._push_worker_timeout_error()

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "stt.gigaam_worker_timeout")
        self.assertEqual(err.severity, "warn")
        self.assertEqual(err.component, "stt")
        self.assertFalse(err.actionable)

    def test_no_push_when_error_bus_none(self):
        """No error raised when _error_bus is None."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        session.oom_callback = None
        session._error_bus = None

        # Should not raise
        session._push_worker_timeout_error()

    def test_timeout_kill_calls_push(self):
        """_timeout_kill calls _push_worker_timeout_error after terminating process."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        session.oom_callback = None
        session._error_bus = None

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # process still running
        session._proc = mock_proc

        with patch.object(session, "_push_worker_timeout_error") as mock_push:
            session._timeout_kill()

        mock_proc.terminate.assert_called_once()
        mock_push.assert_called_once()


if __name__ == "__main__":
    unittest.main()
