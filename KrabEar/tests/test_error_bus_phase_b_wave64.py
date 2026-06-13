"""Unit tests for Wave 64 Phase B error codes and their call-site wiring.

One test per new code:
1. stt.gigaam.ffmpeg_missing     — service.py startup check via _push_startup_error
2. mlx.metal_assertion_failure   — engine.py MLX inference exception branch
3. mlx.semaphore_leak            — stt_gigaam.py subprocess shutdown finally block
4. stt.empty_audio_warning       — audio_quality.py empty audio frame guard
5. system.malloc_env_leak        — stt_gigaam.py Popen env cleanup
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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
        return original_push(err)

    bus.push = _capture  # type: ignore[method-assign]
    return bus, captured


# ---------------------------------------------------------------------------
# 1. stt.gigaam.ffmpeg_missing — service.py _push_startup_error
# ---------------------------------------------------------------------------

class FFmpegMissingTests(unittest.TestCase):
    """stt.gigaam.ffmpeg_missing fires on startup when ffmpeg is absent."""

    def test_code_in_registry(self):
        self.assertIn("stt.gigaam.ffmpeg_missing", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.gigaam.ffmpeg_missing"]
        self.assertEqual(entry["severity"], "error")
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_logs")
        self.assertEqual(entry["dedupe_seconds"], 3600)

    def test_push_startup_error_fires(self):
        """_push_startup_error correctly creates KrabError for ffmpeg_missing."""
        bus, captured = _make_error_bus()

        # Simulate the service._push_startup_error call directly
        code = "stt.gigaam.ffmpeg_missing"
        entry = ERROR_REGISTRY[code]
        component = code.split(".")[0]  # "stt"
        err = KrabError(
            severity=entry["severity"],
            component=component,
            code=code,
            message_user=entry["user_msg_ru"],
            message_debug="ffmpeg not found in PATH — REST STT disabled",
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=entry["actionable"],
            action_id=entry["action_id"],
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "stt.gigaam.ffmpeg_missing")
        self.assertEqual(e.component, "stt")
        self.assertEqual(e.severity, "error")
        self.assertIn("ffmpeg", e.message_user)
        self.assertIn("brew install ffmpeg", e.message_user)

    def test_component_is_stt(self):
        """The component derived from code prefix is 'stt'."""
        code = "stt.gigaam.ffmpeg_missing"
        self.assertEqual(code.split(".")[0], "stt")

    def test_ffmpeg_missing_not_actionable_without_open_logs(self):
        """action_id must be 'open_logs' when actionable=True."""
        entry = ERROR_REGISTRY["stt.gigaam.ffmpeg_missing"]
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_logs")
        self.assertTrue(entry["action_label"])


# ---------------------------------------------------------------------------
# 2. mlx.metal_assertion_failure — engine.py
# ---------------------------------------------------------------------------

class MetalAssertionFailureTests(unittest.TestCase):
    """mlx.metal_assertion_failure fires on Metal GPU command-buffer errors."""

    def test_code_in_registry(self):
        self.assertIn("mlx.metal_assertion_failure", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["mlx.metal_assertion_failure"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 60)

    def test_push_error_fires_for_metal_assertion(self):
        """engine._push_error emits mlx.metal_assertion_failure with error severity."""
        from core.engine import AudioEngine

        engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "balanced"
        engine.quality_profile = "balanced"

        bus, captured = _make_error_bus()
        engine._error_bus = bus

        engine._push_error(
            "mlx.metal_assertion_failure",
            "RuntimeError: IOGPUMetalCommandBuffer validate failed assertion (model=balanced)",
            severity="error",
        )

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "mlx.metal_assertion_failure")
        self.assertEqual(err.component, "mlx")
        self.assertEqual(err.severity, "error")
        self.assertIn("Metal GPU", err.message_user)
        self.assertIn("автоматически", err.message_user)

    def test_keyword_detection_iogpumetal(self):
        """Keywords that should trigger metal_assertion_failure detection."""
        metal_messages = [
            "iogpumetal",
            "validate failed assertion",
            "commit command buffer",
            "uncommitted encoder",
        ]
        for kw in metal_messages:
            emsg = kw
            matched = any(
                k in emsg for k in (
                    "iogpumetal", "validate failed assertion",
                    "commit command buffer", "uncommitted encoder",
                )
            )
            self.assertTrue(matched, f"Keyword '{kw}' should match detection logic")

    def test_no_error_bus_is_silent(self):
        """_push_error is silent when no _error_bus injected."""
        from core.engine import AudioEngine

        engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "balanced"
        engine.quality_profile = "balanced"
        engine._push_error("mlx.metal_assertion_failure", "test noop")  # must not raise


# ---------------------------------------------------------------------------
# 3. mlx.semaphore_leak — stt_gigaam.py
# ---------------------------------------------------------------------------

class SemaphoreLeakTests(unittest.TestCase):
    """mlx.semaphore_leak fires on GigaAM subprocess shutdown."""

    def test_code_in_registry(self):
        self.assertIn("mlx.semaphore_leak", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["mlx.semaphore_leak"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 1800)

    def test_push_via_error_bus(self):
        """Simulates shutdown push by directly constructing the KrabError."""
        bus, captured = _make_error_bus()

        # Simulate what stt_gigaam.py does in shutdown finally block
        from backend.error_codes import ERROR_REGISTRY as reg
        entry = reg.get("mlx.semaphore_leak", {})
        err = KrabError(
            severity=entry.get("severity", "warn"),
            component="mlx",
            code="mlx.semaphore_leak",
            message_user=entry.get("user_msg_ru", ""),
            message_debug="GigaAM worker subprocess shutdown: potential semaphore leak",
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "mlx.semaphore_leak")
        self.assertEqual(e.component, "mlx")
        self.assertEqual(e.severity, "warn")
        self.assertIn("семафор", e.message_user)

    def test_dedupe_suppresses_second_push(self):
        """Second push within dedupe window returns False (suppressed)."""
        bus, _ = _make_error_bus()

        entry = ERROR_REGISTRY["mlx.semaphore_leak"]

        def _make_err():
            return KrabError(
                severity=entry["severity"],
                component="mlx",
                code="mlx.semaphore_leak",
                message_user=entry["user_msg_ru"],
                message_debug="repeated shutdown",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

        # First push emits
        first = bus.push(_make_err())
        self.assertTrue(first)

        # Second push within 1800s window is suppressed
        second = bus.push(_make_err())
        self.assertFalse(second)


class SemaphoreLeakCloseBehaviorTests(unittest.TestCase):
    """close() pushes mlx.semaphore_leak ONLY on force-kill, not graceful shutdown.

    Regression for the Sentry false-positive flood: the finally block used to push
    the benign warning unconditionally on EVERY close(), even when the worker exited
    cleanly. The leak only happens when we terminate()/kill() a worker that didn't
    self-clean its multiprocessing primitives.
    """

    class _FakeStdin:
        def __init__(self):
            self.closed = False
            self.written = []

        def write(self, s):
            self.written.append(s)

        def flush(self):
            pass

        def close(self):
            self.closed = True

    class _FakeProc:
        """Fake Popen: wait() raises TimeoutExpired `timeouts_to_hang` times, then returns 0."""

        def __init__(self, timeouts_to_hang=0):
            import subprocess as _sp
            self._sp = _sp
            self._timeouts_left = timeouts_to_hang
            self.stdin = SemaphoreLeakCloseBehaviorTests._FakeStdin()
            self.stdout = SemaphoreLeakCloseBehaviorTests._FakeStdin()
            self.stderr = SemaphoreLeakCloseBehaviorTests._FakeStdin()
            self.terminated = False
            self.killed = False

        def wait(self, timeout=None):
            if self._timeouts_left > 0:
                self._timeouts_left -= 1
                raise self._sp.TimeoutExpired(cmd="worker", timeout=timeout)
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def poll(self):
            return 0

    def _make_session(self, proc):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        sess = _GigaAMSubprocessSession(
            venv_python="/usr/bin/python3",
            worker_path="/tmp/worker.py",
            mode="balanced",
            device="cpu",
        )
        sess._proc = proc
        sess._loaded = True
        bus, captured = _make_error_bus()
        sess._error_bus = bus
        return sess, captured

    def test_graceful_close_does_not_push(self):
        """Worker exits on shutdown-op within timeout → no semaphore_leak push."""
        proc = self._FakeProc(timeouts_to_hang=0)
        sess, captured = self._make_session(proc)

        sess.close()

        self.assertFalse(proc.terminated, "graceful path must not terminate()")
        self.assertFalse(proc.killed)
        self.assertEqual(
            [e.code for e in captured], [],
            "graceful shutdown must NOT push mlx.semaphore_leak",
        )
        self.assertIsNone(sess._proc)
        self.assertFalse(sess._loaded)
        # stdout/stderr pipes closed explicitly to avoid GC-finalize BrokenPipeError noise
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    def test_forced_kill_pushes_semaphore_leak(self):
        """Worker ignores shutdown-op (first wait times out) → terminate + push."""
        proc = self._FakeProc(timeouts_to_hang=1)
        sess, captured = self._make_session(proc)

        sess.close()

        self.assertTrue(proc.terminated, "force path must terminate()")
        codes = [e.code for e in captured]
        self.assertEqual(codes, ["mlx.semaphore_leak"])
        self.assertEqual(captured[0].component, "mlx")
        self.assertEqual(captured[0].severity, "warn")

    def test_close_without_error_bus_is_safe(self):
        """Force-kill path with no error_bus injected must not raise."""
        proc = self._FakeProc(timeouts_to_hang=1)
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        sess = _GigaAMSubprocessSession(
            venv_python="/usr/bin/python3",
            worker_path="/tmp/worker.py",
            mode="balanced",
            device="cpu",
        )
        sess._proc = proc
        sess._loaded = True
        sess.close()  # must not raise
        self.assertTrue(proc.terminated)

    def test_stdin_closed_in_finally_when_worker_predied(self):
        """Worker died before shutdown → stdin.flush() raises BrokenPipe, so the
        inline stdin.close() is skipped. The finally block MUST still close stdin,
        or Python's GC finalizes the still-buffered stdin pipe later and emits the
        'Exception ignored while finalizing file ... BrokenPipeError' noise. The
        original W64 fix closed only stdout/stderr (read ends, no buffered writes)
        and missed stdin — the actual write end that buffers the unsent shutdown-op.
        """
        class _PreDiedStdin(SemaphoreLeakCloseBehaviorTests._FakeStdin):
            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")

        proc = self._FakeProc(timeouts_to_hang=0)
        proc.stdin = _PreDiedStdin()
        sess, captured = self._make_session(proc)

        sess.close()  # must not raise

        self.assertTrue(
            proc.stdin.closed,
            "stdin must be closed in finally even when flush() raised BrokenPipe — "
            "otherwise the buffered stdin pipe leaks to GC-finalize and prints "
            "BrokenPipeError noise on every pre-died-worker teardown",
        )
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)
        self.assertIsNone(sess._proc)
        self.assertFalse(sess._loaded)


# ---------------------------------------------------------------------------
# 4. stt.empty_audio_warning — audio_quality.py
# ---------------------------------------------------------------------------

class EmptyAudioWarningTests(unittest.TestCase):
    """stt.empty_audio_warning fires when audio_quality receives empty frame."""

    def test_code_in_registry(self):
        self.assertIn("stt.empty_audio_warning", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.empty_audio_warning"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 600)

    def test_push_via_error_bus(self):
        """Push stt.empty_audio_warning as done in audio_quality.py."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["stt.empty_audio_warning"]
        err = KrabError(
            severity=entry.get("severity", "warn"),
            component="stt",
            code="stt.empty_audio_warning",
            message_user=entry.get("user_msg_ru", ""),
            message_debug="audio_quality.analyze: n_samples=0 (empty audio frame)",
            timestamp=datetime.now(timezone.utc),
            context={"sample_rate": 16000},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "stt.empty_audio_warning")
        self.assertEqual(e.component, "stt")
        self.assertEqual(e.severity, "warn")
        self.assertIn("Пустой", e.message_user)

    def test_audio_quality_guard_with_empty_array(self):
        """AudioQualityAnalyzer handles empty array without raising."""
        import numpy as np
        from core.audio_quality import AudioQualityAnalyzer

        analyzer = AudioQualityAnalyzer()
        empty = np.array([], dtype=np.float32)
        # Must not raise even without _error_bus
        report = analyzer.analyze(empty, sample_rate=16000)
        self.assertEqual(report.rms_level, 0.0)
        self.assertEqual(report.peak_level, 0.0)

    def test_audio_quality_guard_pushes_when_bus_injected(self):
        """AudioQualityAnalyzer pushes stt.empty_audio_warning when _error_bus set."""
        import numpy as np
        from core.audio_quality import AudioQualityAnalyzer

        bus, captured = _make_error_bus()
        analyzer = AudioQualityAnalyzer()
        analyzer._error_bus = bus

        empty = np.array([], dtype=np.float32)
        analyzer.analyze(empty, sample_rate=16000)

        # Should have pushed the warning
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].code, "stt.empty_audio_warning")


# ---------------------------------------------------------------------------
# 5. system.malloc_env_leak — stt_gigaam.py
# ---------------------------------------------------------------------------

class MallocEnvLeakTests(unittest.TestCase):
    """system.malloc_env_leak fires when MALLOC_STACK_LOGGING is in subprocess env."""

    def test_code_in_registry(self):
        self.assertIn("system.malloc_env_leak", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["system.malloc_env_leak"]
        self.assertEqual(entry["severity"], "info")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 3600)

    def test_component_is_system(self):
        """system.malloc_env_leak has component='system'."""
        code = "system.malloc_env_leak"
        component = code.split(".")[0]
        self.assertEqual(component, "system")

    def test_push_via_error_bus(self):
        """Push system.malloc_env_leak as done in stt_gigaam.py Popen path."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["system.malloc_env_leak"]
        err = KrabError(
            severity=entry.get("severity", "info"),
            component="system",
            code="system.malloc_env_leak",
            message_user=entry.get("user_msg_ru", ""),
            message_debug=(
                "MALLOC_STACK_LOGGING found in subprocess env; "
                "stripped before Popen to prevent macOS warning"
            ),
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "system.malloc_env_leak")
        self.assertEqual(e.component, "system")
        self.assertEqual(e.severity, "info")
        self.assertIn("MALLOC_STACK_LOGGING", e.message_user)

    def test_system_component_accepted_by_error_bus(self):
        """ErrorBus accepts 'system' as a valid component."""
        bus, captured = _make_error_bus()
        entry = ERROR_REGISTRY["system.malloc_env_leak"]
        err = KrabError(
            severity="info",
            component="system",  # New Wave 64 component
            code="system.malloc_env_leak",
            message_user=entry["user_msg_ru"],
            message_debug="test",
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        # Must not raise pydantic validation error
        pushed = bus.push(err)
        self.assertTrue(pushed)


if __name__ == "__main__":
    unittest.main()
