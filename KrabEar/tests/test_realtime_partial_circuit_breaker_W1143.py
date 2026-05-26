"""Tests for RealtimePartialTranscriber circuit breaker (W1135 F3 LOW).

Verifies:
- After _MAX_CONSECUTIVE_ERRORS consecutive errors the worker exits the loop.
- The worker emits ``realtime.partial_disabled`` on circuit-break exit.
- The error counter resets to 0 on a successful transcription cycle.
"""

from __future__ import annotations

import sys
import os
import time
import threading
import unittest

# Resolve backend package from repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_KRABEAR_ROOT = os.path.join(_REPO_ROOT, "KrabEar")
for _p in (_KRABEAR_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.realtime_partial import (  # noqa: E402
    RealtimePartialTranscriber,
    _MAX_CONSECUTIVE_ERRORS,
    _REALTIME_PARTIAL_DISABLED_TYPE,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeRecorder:
    """Raises RuntimeError on every call (simulates permanent GPU hang)."""

    def __init__(self, raise_exc: bool = True):
        self._raise = raise_exc
        self._call_count = 0

    def snapshot_audio(self, max_duration_sec: float = 8.0):  # noqa: D102
        self._call_count += 1
        if self._raise:
            raise RuntimeError("GPU hang — simulated permanent error")
        # Plain list — no numpy needed; getattr(audio, "size", None) → None → passes check.
        audio = [0.0] * 160
        return audio, 1.0


class _FakeTranscriber:
    """Returns a non-empty text result."""

    def transcribe_preview(self, audio_data, quality_profile="balanced"):  # noqa: D102
        return {"text": "hello world"}


class _AlwaysFailTranscriber:
    """Raises on every transcribe_preview call."""

    def transcribe_preview(self, audio_data, quality_profile="balanced"):  # noqa: D102
        raise RuntimeError("STT engine crashed")


class _FakeEventBus:
    """Captures all emitted events."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, data: dict) -> None:
        with self._lock:
            self.events.append((event_type, data))

    def get_by_type(self, event_type: str) -> list[dict]:
        with self._lock:
            return [d for t, d in self.events if t == event_type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCircuitBreakerExitsAfter10Errors(unittest.TestCase):
    """Worker must exit its loop after _MAX_CONSECUTIVE_ERRORS consecutive errors."""

    def _run_until_stopped(
        self,
        transcriber: RealtimePartialTranscriber,
        session_id: str = "sess-cb-test",
        timeout: float = 10.0,
    ) -> None:
        """Start the transcriber, wait until the thread dies or timeout."""
        transcriber.start(session_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not transcriber.is_running:
                return
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # snapshot_audio always fails
    # ------------------------------------------------------------------

    def test_circuit_breaker_exits_after_10_snapshot_errors(self):
        """Worker exits when snapshot_audio raises 10 times in a row."""
        recorder = _FakeRecorder(raise_exc=True)
        transcriber_obj = _FakeTranscriber()
        bus = _FakeEventBus()

        t = RealtimePartialTranscriber(
            transcriber=transcriber_obj,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.01,  # fast iteration
            buffer_sec=1.0,
        )
        self._run_until_stopped(t, timeout=5.0)

        self.assertFalse(
            t.is_running,
            "Worker thread should have exited after 10 consecutive snapshot errors",
        )

    def test_circuit_breaker_emits_partial_disabled_event(self):
        """Worker emits realtime.partial_disabled when circuit breaks."""
        recorder = _FakeRecorder(raise_exc=True)
        transcriber_obj = _FakeTranscriber()
        bus = _FakeEventBus()

        t = RealtimePartialTranscriber(
            transcriber=transcriber_obj,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.01,
            buffer_sec=1.0,
        )
        self._run_until_stopped(t, timeout=5.0)

        disabled_events = bus.get_by_type(_REALTIME_PARTIAL_DISABLED_TYPE)
        self.assertTrue(
            len(disabled_events) >= 1,
            f"Expected at least one {_REALTIME_PARTIAL_DISABLED_TYPE} event, got: {bus.events}",
        )
        payload = disabled_events[0]
        self.assertEqual(payload["reason"], "consecutive_errors")
        self.assertEqual(payload["error_count"], _MAX_CONSECUTIVE_ERRORS)
        self.assertIn("session_id", payload)
        self.assertIn("ts", payload)

    # ------------------------------------------------------------------
    # transcribe_preview always fails
    # ------------------------------------------------------------------

    def test_circuit_breaker_exits_after_10_transcribe_errors(self):
        """Worker exits when transcribe_preview raises 10 times in a row."""
        recorder = _FakeRecorder(raise_exc=False)
        transcriber_obj = _AlwaysFailTranscriber()
        bus = _FakeEventBus()

        t = RealtimePartialTranscriber(
            transcriber=transcriber_obj,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.01,
            buffer_sec=1.0,
        )
        self._run_until_stopped(t, timeout=5.0)

        self.assertFalse(t.is_running)
        disabled_events = bus.get_by_type(_REALTIME_PARTIAL_DISABLED_TYPE)
        self.assertTrue(len(disabled_events) >= 1)


class TestCircuitBreakerResetsOnSuccess(unittest.TestCase):
    """Error counter must reset to 0 when a cycle succeeds."""

    def test_counter_resets_on_success(self):
        """If errors occur then a success happens, counter resets — worker keeps running."""

        call_count = {"n": 0}

        class _PartialFailRecorder:
            """Raises for first 5 calls, succeeds on 6th+ (uses plain list, no numpy)."""

            def snapshot_audio(self, max_duration_sec=8.0):  # noqa: D102
                call_count["n"] += 1
                if call_count["n"] <= 5:
                    raise RuntimeError("temporary error")
                # Return a plain list; size check uses getattr(audio, "size", None)
                # which returns None for a list → buffer not skipped.
                audio = [0.0] * 160
                return audio, float(call_count["n"])

        recorder = _PartialFailRecorder()
        transcriber_obj = _FakeTranscriber()
        bus = _FakeEventBus()

        t = RealtimePartialTranscriber(
            transcriber=transcriber_obj,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.01,
            buffer_sec=1.0,
        )
        t.start("sess-reset-test")

        # Wait up to 3 s for at least one partial_transcript event (proves success + reset).
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if bus.get_by_type("realtime.partial_transcript"):
                break
            time.sleep(0.05)

        t.stop(timeout_sec=1.0)

        partial_events = bus.get_by_type("realtime.partial_transcript")
        self.assertTrue(
            len(partial_events) >= 1,
            "Expected at least one partial_transcript event after error counter reset",
        )
        # No circuit-break event should have been emitted.
        disabled_events = bus.get_by_type(_REALTIME_PARTIAL_DISABLED_TYPE)
        self.assertEqual(
            len(disabled_events),
            0,
            f"Worker should NOT have emitted partial_disabled; got: {disabled_events}",
        )

    def test_constant_below_max_value(self):
        """_MAX_CONSECUTIVE_ERRORS must equal 10 as per spec."""
        self.assertEqual(_MAX_CONSECUTIVE_ERRORS, 10)


if __name__ == "__main__":
    unittest.main()
