"""Tests for Phase B.2 F11 — worker subprocess OOM detection.

Tests cover:
- _detect_subprocess_oom heuristics (returncode -6/-9, stderr patterns)
- _push_mlx_oom_for_worker fires mlx.oom via ErrorBus
- _GigaAMSubprocessSession._check_proc_oom_on_exit fires oom_callback
- Normal (non-OOM) exits do not trigger callbacks

IMPORTANT: DO NOT import mlx_whisper or gigaam — memory constraint.
All subprocess interaction is mocked.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Import helpers under test
# ---------------------------------------------------------------------------

from core.engine import AudioEngine
from core.pipeline.stt_gigaam import detect_subprocess_oom, _GigaAMSubprocessSession


# ---------------------------------------------------------------------------
# detect_subprocess_oom — pure function tests (no process, no MLX)
# ---------------------------------------------------------------------------

class DetectSubprocessOomTests(unittest.TestCase):
    """Unit tests for detect_subprocess_oom heuristic.

    Function now returns tuple[bool, str | None] — (is_oom, signal_name).
    """

    def test_returncode_minus_6_detected_as_oom(self) -> None:
        """SIGABRT (-6) must be detected as OOM with signal name SIGABRT."""
        is_oom, signal_name = detect_subprocess_oom(-6, "")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "SIGABRT")

    def test_returncode_minus_9_is_not_proven_mlx_oom(self) -> None:
        """SIGKILL (-9) is process-killed (jetsam/внешний kill), не доказанный Metal OOM."""
        is_oom, signal_name = detect_subprocess_oom(-9, "")
        self.assertFalse(is_oom)
        self.assertEqual(signal_name, "SIGKILL")

    def test_sigkill_with_oom_stderr_is_still_oom(self) -> None:
        """Текстовая улика OOM важнее голого SIGKILL — тогда это mlx OOM."""
        is_oom, signal_name = detect_subprocess_oom(-9, "Metal out of memory: allocation failed")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "stderr_oom_pattern")

    def test_returncode_minus_11_detected_as_oom_with_sigsegv(self) -> None:
        """SIGSEGV (-11) must be detected as OOM with signal name SIGSEGV."""
        is_oom, signal_name = detect_subprocess_oom(-11, "")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "SIGSEGV")

    def test_returncode_minus_10_detected_as_oom_with_sigbus(self) -> None:
        """SIGBUS (-10) must be detected as OOM with signal name SIGBUS."""
        is_oom, signal_name = detect_subprocess_oom(-10, "")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "SIGBUS")

    def test_returncode_zero_returns_false_with_none(self) -> None:
        """Clean exit (0) must return (False, None)."""
        is_oom, signal_name = detect_subprocess_oom(0, "")
        self.assertFalse(is_oom)
        self.assertIsNone(signal_name)

    def test_returncode_1_not_oom(self) -> None:
        """Normal error exit (1) must not be detected as OOM."""
        is_oom, signal_name = detect_subprocess_oom(1, "")
        self.assertFalse(is_oom)
        self.assertIsNone(signal_name)

    def test_returncode_minus_15_not_oom(self) -> None:
        """SIGTERM (-15) is not OOM (graceful termination)."""
        is_oom, signal_name = detect_subprocess_oom(-15, "")
        self.assertFalse(is_oom)
        self.assertIsNone(signal_name)

    def test_stderr_out_of_memory_detected(self) -> None:
        """stderr containing 'out of memory' must be detected as OOM."""
        is_oom, signal_name = detect_subprocess_oom(1, "Python: out of memory\n")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "stderr_oom_pattern")

    def test_stderr_outofmemoryerror_detected(self) -> None:
        """OutOfMemoryError in stderr must be detected as OOM."""
        is_oom, signal_name = detect_subprocess_oom(1, "OutOfMemoryError: torch.mps")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "stderr_oom_pattern")

    def test_stderr_metal_out_of_memory_detected(self) -> None:
        """Metal out of memory in stderr must be detected as OOM."""
        is_oom, signal_name = detect_subprocess_oom(1, "Metal out of memory: allocation failed")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "stderr_oom_pattern")

    def test_stderr_mallocstacklogging_detected(self) -> None:
        """MallocStackLogging in stderr (macOS kernel OOM trace) must be detected."""
        is_oom, signal_name = detect_subprocess_oom(1, "MallocStackLogging: recording stacks")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "stderr_oom_pattern")

    def test_stderr_oom_returns_true_with_stderr_oom_pattern(self) -> None:
        """Any OOM stderr pattern must return (True, 'stderr_oom_pattern')."""
        is_oom, signal_name = detect_subprocess_oom(1, "OUT OF MEMORY")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "stderr_oom_pattern")

    def test_stderr_case_insensitive(self) -> None:
        """Pattern matching must be case-insensitive."""
        is_oom1, _ = detect_subprocess_oom(1, "OUT OF MEMORY")
        is_oom2, _ = detect_subprocess_oom(1, "Metal Out Of Memory")
        self.assertTrue(is_oom1)
        self.assertTrue(is_oom2)

    def test_stderr_normal_exit_not_oom(self) -> None:
        """Normal stderr content must not trigger OOM detection."""
        is_oom, signal_name = detect_subprocess_oom(0, "gigaam_worker: started\ngigaam_worker: exiting\n")
        self.assertFalse(is_oom)
        self.assertIsNone(signal_name)

    def test_empty_stderr_no_oom_for_normal_exit(self) -> None:
        """Empty stderr with normal exit must not trigger OOM."""
        is_oom, signal_name = detect_subprocess_oom(0, "")
        self.assertFalse(is_oom)
        self.assertIsNone(signal_name)

    def test_none_like_empty_stderr_no_crash(self) -> None:
        """Empty string stderr does not crash the function."""
        is_oom, signal_name = detect_subprocess_oom(1, "")
        self.assertFalse(is_oom)
        self.assertIsNone(signal_name)

    def test_returncode_minus_6_takes_priority_over_empty_stderr(self) -> None:
        """returncode -6 must trigger OOM even without OOM keywords in stderr."""
        is_oom, signal_name = detect_subprocess_oom(-6, "gigaam_worker: started\n")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "SIGABRT")


# ---------------------------------------------------------------------------
# AudioEngine._detect_subprocess_oom — static method alias on engine
# ---------------------------------------------------------------------------

class AudioEngineDetectOomStaticTests(unittest.TestCase):
    """AudioEngine._detect_subprocess_oom must delegate to same logic (returns tuple)."""

    def test_static_method_minus_6(self) -> None:
        is_oom, signal_name = AudioEngine._detect_subprocess_oom(-6, "")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "SIGABRT")

    def test_static_method_minus_9_is_not_proven_oom(self) -> None:
        is_oom, signal_name = AudioEngine._detect_subprocess_oom(-9, "")
        self.assertFalse(is_oom)
        self.assertEqual(signal_name, "SIGKILL")

    def test_static_method_stderr_pattern(self) -> None:
        is_oom, signal_name = AudioEngine._detect_subprocess_oom(1, "out of memory: Metal")
        self.assertTrue(is_oom)
        self.assertEqual(signal_name, "stderr_oom_pattern")

    def test_static_method_normal_exit(self) -> None:
        is_oom, signal_name = AudioEngine._detect_subprocess_oom(0, "clean exit")
        self.assertFalse(is_oom)
        self.assertIsNone(signal_name)


# ---------------------------------------------------------------------------
# AudioEngine._push_mlx_oom_for_worker — fires mlx.oom via ErrorBus
# ---------------------------------------------------------------------------

def _make_engine_stub() -> AudioEngine:
    """Build minimal AudioEngine stub without triggering model loading."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.current_model = "mlx-community/whisper-base-mlx"
    engine.quality_profile = "balanced"
    engine._unavailable_models = {}
    engine._error_bus = MagicMock()
    engine._llm_rewriter = None
    engine._settings_get = lambda k, d: d
    return engine


class PushMlxOomForWorkerTests(unittest.TestCase):
    """Tests for AudioEngine._push_mlx_oom_for_worker."""

    def test_fires_error_bus_with_mlx_oom_code(self) -> None:
        """_push_mlx_oom_for_worker must push mlx.oom code."""
        engine = _make_engine_stub()
        engine._push_mlx_oom_for_worker("gigaam_worker", -6, "some stderr")
        self.assertEqual(engine._error_bus.push.call_count, 1)
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "mlx.oom")

    def test_fires_with_critical_severity(self) -> None:
        """mlx.oom must be pushed with critical severity."""
        engine = _make_engine_stub()
        engine._push_mlx_oom_for_worker("gigaam_worker", -6, "")
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.severity, "critical")
        self.assertEqual(pushed.code, "mlx.oom")

    def test_sigkill_does_not_push_mlx_oom(self) -> None:
        """Голый SIGKILL не должен эмитить mlx.oom — это врёт тосту и OOM-релифу."""
        engine = _make_engine_stub()
        engine._push_mlx_oom_for_worker("gigaam_worker", -9, "")
        self.assertEqual(engine._error_bus.push.call_count, 1)
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertNotEqual(pushed.code, "mlx.oom")
        self.assertEqual(pushed.code, "stt.worker_killed")
        self.assertIn("SIGKILL", pushed.message_debug)

    def test_debug_message_contains_name_and_rc(self) -> None:
        """message_debug must contain worker name, returncode, and signal name."""
        engine = _make_engine_stub()
        engine._push_mlx_oom_for_worker("gigaam_worker", -6, "stderr output")
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertIn("gigaam_worker", pushed.message_debug)
        self.assertIn("-6", pushed.message_debug)
        self.assertIn("SIGABRT", pushed.message_debug)

    def test_no_bus_does_not_raise(self) -> None:
        """_push_mlx_oom_for_worker must not raise when no _error_bus."""
        engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "model"
        engine.quality_profile = "balanced"
        # No _error_bus
        engine._push_mlx_oom_for_worker("gigaam_worker", -6, "oom")  # must not raise

    def test_broken_bus_does_not_raise(self) -> None:
        """_push_mlx_oom_for_worker must not raise when push raises."""
        engine = _make_engine_stub()
        engine._error_bus.push.side_effect = RuntimeError("bus broken")
        engine._push_mlx_oom_for_worker("gigaam_worker", -9, "oom")  # must not raise


# ---------------------------------------------------------------------------
# _GigaAMSubprocessSession._check_proc_oom_on_exit — callback wiring
# ---------------------------------------------------------------------------

class SubprocessSessionOomCallbackTests(unittest.TestCase):
    """Tests for _GigaAMSubprocessSession._check_proc_oom_on_exit."""

    def _make_session(self) -> _GigaAMSubprocessSession:
        """Build a session with mocked _proc (no real subprocess)."""
        session = _GigaAMSubprocessSession(
            venv_python="/fake/python",
            worker_path="/fake/worker.py",
            mode="rnnt",
            device="cpu",
        )
        return session

    def test_oom_callback_fired_on_sigabrt(self) -> None:
        """oom_callback must be called when proc exits with returncode -6."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = -6
        mock_proc.stderr.read.return_value = ""
        session._proc = mock_proc

        session._check_proc_oom_on_exit()
        callback.assert_called_once_with("gigaam_worker", -6, "")

    def test_oom_callback_fired_on_sigkill(self) -> None:
        """oom_callback must be called when proc exits with returncode -9."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = -9
        mock_proc.stderr.read.return_value = "kernel: memory pressure"
        session._proc = mock_proc

        session._check_proc_oom_on_exit()
        callback.assert_called_once()
        args = callback.call_args[0]
        self.assertEqual(args[0], "gigaam_worker")
        self.assertEqual(args[1], -9)

    def test_oom_callback_fired_on_stderr_pattern(self) -> None:
        """oom_callback must be called when stderr contains OOM pattern."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # non-OOM rc, but OOM in stderr
        mock_proc.stderr.read.return_value = "RuntimeError: out of memory\n"
        session._proc = mock_proc

        session._check_proc_oom_on_exit()
        callback.assert_called_once()

    def test_no_callback_on_normal_exit(self) -> None:
        """oom_callback must NOT be called for normal exit (rc=0)."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stderr.read.return_value = "gigaam_worker: done\n"
        session._proc = mock_proc

        session._check_proc_oom_on_exit()
        callback.assert_not_called()

    def test_no_callback_on_still_running(self) -> None:
        """oom_callback must NOT be called when process is still alive (poll=None)."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        session._proc = mock_proc

        session._check_proc_oom_on_exit()
        callback.assert_not_called()

    def test_no_callback_set_does_not_raise(self) -> None:
        """_check_proc_oom_on_exit must not raise when no oom_callback set."""
        session = self._make_session()
        # No oom_callback set

        mock_proc = MagicMock()
        mock_proc.poll.return_value = -6
        mock_proc.stderr.read.return_value = ""
        session._proc = mock_proc

        session._check_proc_oom_on_exit()  # must not raise

    def test_no_proc_does_not_raise(self) -> None:
        """_check_proc_oom_on_exit must not raise when _proc is None."""
        session = self._make_session()
        session._proc = None
        session._check_proc_oom_on_exit()  # must not raise

    def test_broken_callback_does_not_raise(self) -> None:
        """_check_proc_oom_on_exit must not raise when oom_callback itself raises."""
        session = self._make_session()
        session.oom_callback = MagicMock(side_effect=RuntimeError("callback crashed"))

        mock_proc = MagicMock()
        mock_proc.poll.return_value = -6
        mock_proc.stderr.read.return_value = ""
        session._proc = mock_proc

        session._check_proc_oom_on_exit()  # must not raise


# ---------------------------------------------------------------------------
# H3 backward-compat: ring-buffer OOM detection preserves F11 behaviour
# ---------------------------------------------------------------------------

class H3RingBufferOomCompatTests(unittest.TestCase):
    """Verify H3 ring-buffer change preserves F11 OOM detection (backward-compat).

    The key guarantee: existing tests that mock proc.stderr.read() must still work
    when the ring buffer is empty (ring-empty fallback path in _check_proc_oom_on_exit).
    When ring is populated (drain thread ran), OOM is still detected from ring content.
    """

    def _make_session(self) -> _GigaAMSubprocessSession:
        session = _GigaAMSubprocessSession(
            venv_python="/fake/python",
            worker_path="/fake/worker.py",
            mode="rnnt",
            device="cpu",
        )
        return session

    def test_ring_empty_fallback_sigabrt_still_detected(self) -> None:
        """With empty ring, SIGABRT (-6) is still detected via returncode (F11 compat)."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = -6
        mock_proc.stderr.read.return_value = ""
        session._proc = mock_proc
        session._stderr_ring.clear()

        session._check_proc_oom_on_exit()
        callback.assert_called_once_with("gigaam_worker", -6, "")

    def test_ring_empty_fallback_sigkill_still_detected(self) -> None:
        """With empty ring, SIGKILL (-9) is still detected via returncode (F11 compat)."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = -9
        mock_proc.stderr.read.return_value = "kernel: memory pressure"
        session._proc = mock_proc
        session._stderr_ring.clear()

        session._check_proc_oom_on_exit()
        callback.assert_called_once()
        self.assertEqual(callback.call_args[0][1], -9)

    def test_ring_populated_oom_pattern_detected(self) -> None:
        """When ring has OOM pattern, callback fires even with empty proc.stderr.read()."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.stderr.read.return_value = ""  # already drained by thread
        session._proc = mock_proc

        # Drain thread would have placed this:
        session._stderr_ring.append("torch.cuda.OutOfMemoryError: out of memory\n")

        session._check_proc_oom_on_exit()
        callback.assert_called_once()
        args = callback.call_args[0]
        self.assertIn("out of memory", args[2].lower())

    def test_ring_normal_exit_does_not_fire_callback(self) -> None:
        """Normal worker output in ring (no OOM keywords) must not trigger callback."""
        session = self._make_session()
        callback = MagicMock()
        session.oom_callback = callback

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stderr.read.return_value = ""
        session._proc = mock_proc

        session._stderr_ring.append("gigaam_worker: started\n")
        session._stderr_ring.append("gigaam_worker: model loaded\n")
        session._stderr_ring.append("gigaam_worker: exiting\n")

        session._check_proc_oom_on_exit()
        callback.assert_not_called()

    def test_ring_has_200_line_cap(self) -> None:
        """_stderr_ring deque must be capped at exactly 200 lines."""
        from collections import deque
        session = self._make_session()
        ring: deque = session._stderr_ring
        self.assertEqual(ring.maxlen, 200, "_stderr_ring maxlen must be 200")

    def test_stderr_drain_thread_attribute_exists(self) -> None:
        """_GigaAMSubprocessSession must have _stderr_drain_thread attribute."""
        session = self._make_session()
        self.assertTrue(
            hasattr(session, "_stderr_drain_thread"),
            "_GigaAMSubprocessSession must have _stderr_drain_thread",
        )
        self.assertIsNone(
            session._stderr_drain_thread,
            "_stderr_drain_thread must be None before start()",
        )

    def test_start_stderr_drain_no_op_when_proc_none(self) -> None:
        """_start_stderr_drain() must not raise and not set thread when proc is None."""
        session = self._make_session()
        session._proc = None
        session._start_stderr_drain()
        self.assertIsNone(session._stderr_drain_thread)

    def test_start_stderr_drain_creates_daemon_thread(self) -> None:
        """_start_stderr_drain() must create a daemon thread when proc is available."""
        import threading
        from unittest.mock import MagicMock

        session = self._make_session()

        mock_proc = MagicMock()
        mock_proc.pid = 42000
        mock_proc.poll.return_value = 0  # already exited → drain loop exits immediately
        mock_proc.stderr.readline.return_value = ""
        mock_proc.stderr.__iter__ = lambda self: iter([])
        session._proc = mock_proc

        session._start_stderr_drain()

        self.assertIsNotNone(session._stderr_drain_thread)
        self.assertIsInstance(session._stderr_drain_thread, threading.Thread)
        self.assertTrue(session._stderr_drain_thread.daemon)


if __name__ == "__main__":
    unittest.main()
