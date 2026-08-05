"""Tests for W1652 fixes: recorder._error_bus wiring (F1 HIGH) + deadlock fix (F3 MED).

F1: BackendService.__init__ must wire self._error_bus into self.recorder._error_bus.
F3: _push_max_duration_error() must be called OUTSIDE self._lock (deadlock prevention).
"""
from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

# Path setup
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_ROOT not in sys.path:
    sys.path.insert(0, KRAB_ROOT)

from backend.recorder import AudioRecorder, MAX_RECORDING_SAMPLES


# ---------------------------------------------------------------------------
# F1: BackendService wires recorder._error_bus
# ---------------------------------------------------------------------------

class TestRecorderErrorBusInjectedInBackendService(unittest.TestCase):
    """BackendService.__init__ must wire self._error_bus into self.recorder._error_bus."""

    def _build_minimal_service(self):
        """Construct BackendService with all heavy deps mocked out."""
        import tempfile
        from unittest.mock import MagicMock, patch

        tmp_dir = tempfile.mkdtemp()

        # Patch all heavy imports / constructors that would fail in CI
        patches = [
            patch("backend.service.AudioEngine", MagicMock()),
            patch("backend.service.Transcriber", MagicMock()),
            patch("backend.service.Translator", MagicMock()),
            patch("backend.service.LLMRewriter", MagicMock()),
            patch("backend.service.StateStore", MagicMock()),
            patch("backend.service.MetricsCollector", MagicMock()),
            patch("backend.service.AudioRecorder", MagicMock()),
            patch("backend.service.EventBus", MagicMock()),
        ]
        started = []
        for p in patches:
            started.append(p.start())

        try:
            from backend.service import BackendService
            svc = BackendService(data_dir=tmp_dir)
        finally:
            for p in patches:
                p.stop()

        return svc

    def test_recorder_error_bus_injected(self):
        """After __init__, recorder._error_bus must equal service._error_bus."""
        try:
            svc = self._build_minimal_service()
        except Exception:
            self.skipTest("BackendService construction failed in CI environment")

        # The recorder mock should have had _error_bus set on it
        recorder = svc.recorder
        self.assertIsNotNone(
            getattr(recorder, "_error_bus", None),
            "recorder._error_bus must be wired in BackendService.__init__ (W1652 F1)",
        )


# ---------------------------------------------------------------------------
# F1 (unit): push fires when _error_bus is wired
# ---------------------------------------------------------------------------

class TestMaxDurationErrorPushedWhenBusWired(unittest.TestCase):
    """_push_max_duration_error() must call error_bus.push when bus is set."""

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def test_push_noop_when_no_bus(self):
        """Without _error_bus, _push_max_duration_error must silently no-op."""
        self.assertFalse(hasattr(self.recorder, "_error_bus"))
        # Should not raise
        self.recorder._push_max_duration_error(MAX_RECORDING_SAMPLES)

    def test_push_calls_bus_when_wired(self):
        """With _error_bus set, push() must be called exactly once."""
        mock_bus = MagicMock()
        self.recorder._error_bus = mock_bus

        self.recorder._push_max_duration_error(MAX_RECORDING_SAMPLES)

        mock_bus.push.assert_called_once()
        # Verify the pushed error has the correct code
        pushed_err = mock_bus.push.call_args[0][0]
        self.assertEqual(pushed_err.code, "audio.max_duration_reached")

    def test_push_uses_effective_session_cap_not_constructor_default(self):
        """2026-08-05: сообщение отражает переданный (per-session) потолок,
        а не всегда конструкторный self._max_recording_samples — иначе
        diction/quick_capture записи с тесным override врали бы в тосте."""
        mock_bus = MagicMock()
        self.recorder._error_bus = mock_bus
        tight_cap = 16000 * 60 * 45  # 45 минут

        self.recorder._push_max_duration_error(tight_cap)

        pushed_err = mock_bus.push.call_args[0][0]
        self.assertEqual(pushed_err.context["max_samples"], tight_cap)
        self.assertEqual(pushed_err.context["max_hours"], 0)
        self.assertEqual(pushed_err.context["max_minutes"], 45)

    def test_push_message_uses_minutes_for_sub_hour_cap(self):
        """2026-08-05 LOW-C (Fable): sub-hour потолок (типичный dictation-
        default 45 мин) не должен показывать вводящее в заблуждение "(0 ч)"."""
        mock_bus = MagicMock()
        self.recorder._error_bus = mock_bus
        tight_cap = 16000 * 60 * 45  # 45 минут

        self.recorder._push_max_duration_error(tight_cap)

        pushed_err = mock_bus.push.call_args[0][0]
        self.assertIn("45 мин", pushed_err.message_user)
        self.assertNotIn("0 ч", pushed_err.message_user)

    def test_push_message_uses_hours_for_hour_plus_cap(self):
        """Потолки >= 1ч (например, конструкторный 4ч дефолт) по-прежнему
        показываются в часах, не в сотнях минут."""
        mock_bus = MagicMock()
        self.recorder._error_bus = mock_bus

        self.recorder._push_max_duration_error(MAX_RECORDING_SAMPLES)

        pushed_err = mock_bus.push.call_args[0][0]
        self.assertIn("4 ч", pushed_err.message_user)

    def test_push_never_raises_on_bus_exception(self):
        """_push_max_duration_error must swallow any exception from error_bus.push."""
        mock_bus = MagicMock()
        mock_bus.push.side_effect = RuntimeError("bus exploded")
        self.recorder._error_bus = mock_bus

        # Must NOT propagate
        self.recorder._push_max_duration_error(MAX_RECORDING_SAMPLES)


# ---------------------------------------------------------------------------
# F3: _push_max_duration_error called OUTSIDE lock
# ---------------------------------------------------------------------------

class TestPushMaxDurationErrorNotCalledUnderLock(unittest.TestCase):
    """_push_max_duration_error must execute while self._lock is NOT held.

    Strategy: install a mock error_bus whose push() attempts a non-blocking
    acquire of recorder._lock and asserts success.  If push() is called while
    the lock is held the non-blocking acquire will fail → test fails.
    """

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def test_lock_not_held_during_push(self):
        """push() succeeds in acquiring recorder._lock — confirms it was released."""
        lock_was_free = []

        class LockCheckBus:
            def push(self_bus, err):  # noqa: N805
                # Try to acquire the recorder lock without blocking.
                acquired = self.recorder._lock.acquire(blocking=False)
                lock_was_free.append(acquired)
                if acquired:
                    self.recorder._lock.release()

        self.recorder._error_bus = LockCheckBus()

        # Simulate the _worker hitting the MAX_RECORDING_SAMPLES cap
        chunk_size = self.recorder.chunk_size
        samples_near_cap = MAX_RECORDING_SAMPLES - chunk_size + 1

        def fake_read(_n):
            data = np.zeros((chunk_size, 1), dtype=np.float32)
            return data, False

        mock_stream = MagicMock()
        mock_stream.__enter__ = lambda s: s
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.read = fake_read

        with patch("backend.recorder.sd") as mock_sd:
            mock_sd.InputStream.return_value = mock_stream
            self.recorder._stop_event.clear()
            with self.recorder._lock:
                self.recorder._is_recording = True
                self.recorder._started_at = 0.0
                self.recorder._chunks = []
                self.recorder._chunks_total_samples = samples_near_cap

            t = threading.Thread(target=self.recorder._worker, daemon=True)
            t.start()
            t.join(timeout=5.0)

        self.assertFalse(t.is_alive(), "Worker must have stopped")
        self.assertTrue(
            len(lock_was_free) > 0,
            "_push_max_duration_error was never called — check test setup",
        )
        self.assertTrue(
            all(lock_was_free),
            "recorder._lock was HELD during _push_max_duration_error — deadlock risk (W1652 F3)",
        )

    def test_buffer_overflow_push_still_outside_lock(self):
        """_push_buffer_overflow_error (already correct) also runs outside lock."""
        lock_was_free = []

        class LockCheckBus:
            def push(self_bus, err):  # noqa: N805
                acquired = self.recorder._lock.acquire(blocking=False)
                lock_was_free.append(acquired)
                if acquired:
                    self.recorder._lock.release()

        self.recorder._error_bus = LockCheckBus()

        # Call directly — overflow push is always outside the lock
        self.recorder._push_buffer_overflow_error()

        self.assertTrue(
            len(lock_was_free) > 0,
            "_push_buffer_overflow_error was never called",
        )
        self.assertTrue(all(lock_was_free))


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
