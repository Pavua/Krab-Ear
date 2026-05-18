"""Tests for RealtimePartialTranscriber (backend/realtime_partial.py)."""

from __future__ import annotations

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock

# Path setup — needed when run standalone from repo root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_KRAB_EAR = os.path.join(_PROJECT_ROOT, "KrabEar")
if _KRAB_EAR not in sys.path:
    sys.path.insert(0, _KRAB_EAR)

import numpy as np

from backend.realtime_partial import (
    RealtimePartialTranscriber,
    _REALTIME_PARTIAL_TYPE,
    _REALTIME_FINAL_TYPE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audio(duration_sec: float = 3.0, sr: int = 16000) -> np.ndarray:
    """Build a tiny float32 audio array representing `duration_sec` of audio."""
    return np.zeros(int(sr * duration_sec), dtype=np.float32)


def _make_recorder(duration_sec: float = 3.0, audio: np.ndarray | None = None) -> MagicMock:
    if audio is None:
        audio = _make_audio(duration_sec)
    recorder = MagicMock()
    recorder.snapshot_audio.return_value = (audio, duration_sec)
    recorder.sample_rate = 16000
    return recorder


def _make_transcriber(text: str = "Привет мир") -> MagicMock:
    transcriber = MagicMock()
    transcriber.transcribe_preview.return_value = {"text": text}
    return transcriber


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = MagicMock()
    return bus


# ---------------------------------------------------------------------------
# TestRealtimePartialTranscriberLifecycle
# ---------------------------------------------------------------------------

class TestRealtimePartialTranscriberLifecycle(unittest.TestCase):

    def test_initial_state_not_running(self):
        """Before start(), is_running must be False."""
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
        )
        self.assertFalse(rpt.is_running)

    def test_is_running_after_start(self):
        """After start(), is_running should be True."""
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
            interval_sec=60.0,  # long interval — won't fire during test
        )
        rpt.start(session_id="test-session")
        try:
            self.assertTrue(rpt.is_running)
        finally:
            rpt.stop(timeout_sec=2.0)

    def test_not_running_after_stop(self):
        """After stop(), is_running must be False."""
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
            interval_sec=60.0,
        )
        rpt.start(session_id="test-session")
        rpt.stop(timeout_sec=2.0)
        self.assertFalse(rpt.is_running)

    def test_start_idempotent(self):
        """Calling start() twice must not launch a second thread."""
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
            interval_sec=60.0,
        )
        rpt.start(session_id="s1")
        first_thread = rpt._thread
        rpt.start(session_id="s2")  # second call — must be no-op
        self.assertIs(rpt._thread, first_thread, "Second start() must not replace thread")
        rpt.stop(timeout_sec=2.0)

    def test_stop_idempotent(self):
        """stop() on a not-started instance must not raise."""
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
        )
        rpt.stop(timeout_sec=1.0)  # should not raise
        self.assertFalse(rpt.is_running)


# ---------------------------------------------------------------------------
# TestRealtimePartialEmission
# ---------------------------------------------------------------------------

class TestRealtimePartialEmission(unittest.TestCase):

    def _run_one_tick(
        self,
        text: str = "Привет",
        duration_sec: float = 3.0,
        session_id: str = "sess-abc",
    ) -> tuple[RealtimePartialTranscriber, MagicMock, MagicMock, MagicMock]:
        """Run the worker for just enough time to fire one interval tick."""
        # Use an Event so we wait *deterministically* for the first emit instead
        # of relying on a fixed sleep.  On loaded CI runners (pytest-xdist, slow
        # Python 3.12 GIL scheduling) a 0.5 s sleep was not always enough.
        emitted = threading.Event()
        bus = _make_bus()
        _original_emit = bus.emit.side_effect

        def _emit_and_signal(*args, **kwargs):
            emitted.set()
            if callable(_original_emit):
                return _original_emit(*args, **kwargs)

        bus.emit.side_effect = _emit_and_signal

        recorder = _make_recorder(duration_sec=duration_sec)
        transcriber = _make_transcriber(text=text)

        # Use a short interval so the test completes quickly.
        # Must be >= 0.1 (module clamp).
        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.1,
            buffer_sec=8.0,
        )
        rpt.start(session_id=session_id)
        # Wait up to 3 s for the first emit, then stop.
        emitted.wait(timeout=3.0)
        rpt.stop(timeout_sec=2.0)
        return rpt, bus, recorder, transcriber

    def test_partial_emitted_at_interval(self):
        """Worker must emit at least one realtime.partial_transcript event."""
        _, bus, _, _ = self._run_one_tick(text="Тест трансляции")
        bus.emit.assert_called()
        # At least one call was for our event type
        partial_calls = [
            c for c in bus.emit.call_args_list
            if c.args[0] == _REALTIME_PARTIAL_TYPE
        ]
        self.assertGreater(len(partial_calls), 0)

    def test_partial_event_has_correct_fields(self):
        """Emitted payload must contain session_id, text, is_partial=True, ts."""
        session_id = "sess-fields"
        text = "Корректный текст"
        _, bus, _, _ = self._run_one_tick(text=text, session_id=session_id)

        partial_calls = [
            c for c in bus.emit.call_args_list
            if c.args[0] == _REALTIME_PARTIAL_TYPE
        ]
        self.assertTrue(partial_calls, "No partial events emitted")
        payload = partial_calls[0].args[1]
        self.assertEqual(payload["session_id"], session_id)
        self.assertEqual(payload["text"], text)
        self.assertTrue(payload["is_partial"])
        self.assertIn("ts", payload)
        self.assertIsInstance(payload["ts"], float)

    def test_no_emit_when_text_empty(self):
        """If transcribe_preview returns empty text, no event should be emitted."""
        bus = _make_bus()
        recorder = _make_recorder(duration_sec=3.0)
        transcriber = MagicMock()
        transcriber.transcribe_preview.return_value = {"text": ""}

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.1,
            buffer_sec=8.0,
        )
        rpt.start(session_id="sess-empty")
        time.sleep(0.5)
        rpt.stop(timeout_sec=2.0)

        partial_calls = [
            c for c in bus.emit.call_args_list
            if c.args[0] == _REALTIME_PARTIAL_TYPE
        ]
        self.assertEqual(len(partial_calls), 0, "Should not emit on empty text")

    def test_no_emit_when_buffer_no_progress(self):
        """If snapshot_audio keeps returning the same duration, only first emit fires."""
        bus = _make_bus()
        # Always return the same duration — after first emit, no new progress.
        audio = _make_audio(2.0)
        recorder = MagicMock()
        recorder.snapshot_audio.return_value = (audio, 2.0)
        transcriber = _make_transcriber(text="Дублирующий текст")

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.1,
            buffer_sec=8.0,
        )
        rpt.start(session_id="sess-nodelta")
        time.sleep(0.6)
        rpt.stop(timeout_sec=2.0)

        partial_calls = [
            c for c in bus.emit.call_args_list
            if c.args[0] == _REALTIME_PARTIAL_TYPE
        ]
        # Should emit exactly once (first tick succeeds, subsequent have no delta)
        self.assertEqual(len(partial_calls), 1)


# ---------------------------------------------------------------------------
# TestRealtimePartialErrorHandling
# ---------------------------------------------------------------------------

class TestRealtimePartialErrorHandling(unittest.TestCase):

    def test_snapshot_error_does_not_crash_worker(self):
        """If snapshot_audio raises, the worker continues and does not crash."""
        bus = _make_bus()
        recorder = MagicMock()
        recorder.snapshot_audio.side_effect = RuntimeError("disk full")

        transcriber = _make_transcriber()

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.1,
            buffer_sec=8.0,
        )
        rpt.start(session_id="sess-err1")
        time.sleep(0.5)
        # Worker must still be alive despite errors
        self.assertTrue(rpt.is_running)
        rpt.stop(timeout_sec=2.0)
        # No partial events emitted (all calls raised)
        self.assertEqual(bus.emit.call_count, 0)

    def test_transcribe_error_does_not_crash_worker(self):
        """If transcribe_preview raises, the worker continues."""
        bus = _make_bus()
        recorder = _make_recorder(duration_sec=4.0)
        transcriber = MagicMock()
        transcriber.transcribe_preview.side_effect = Exception("GPU OOM")

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.1,
            buffer_sec=8.0,
        )
        rpt.start(session_id="sess-err2")
        time.sleep(0.5)
        self.assertTrue(rpt.is_running)
        rpt.stop(timeout_sec=2.0)

    def test_disabled_flag_no_start(self):
        """When realtime_partial_enabled=False, the transcriber is never started.

        This is a unit test for the flag logic — we simply verify that if
        the caller never calls start(), is_running is always False.
        """
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
        )
        # Simulate disabled: caller never calls start()
        self.assertFalse(rpt.is_running)


# ---------------------------------------------------------------------------
# TestRealtimePartialSessionIsolation
# ---------------------------------------------------------------------------

class TestRealtimePartialSessionIsolation(unittest.TestCase):

    def test_session_id_in_payload(self):
        """Each session must carry its own session_id in emitted payloads."""
        bus = _make_bus()
        recorder = _make_recorder(duration_sec=3.0)
        transcriber = _make_transcriber(text="Текст 1")

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.1,
        )
        rpt.start(session_id="unique-session-xyz")
        time.sleep(0.5)
        rpt.stop(timeout_sec=2.0)

        partial_calls = [
            c for c in bus.emit.call_args_list
            if c.args[0] == _REALTIME_PARTIAL_TYPE
        ]
        self.assertTrue(partial_calls)
        for c in partial_calls:
            self.assertEqual(c.args[1]["session_id"], "unique-session-xyz")

    def test_sample_rate_stored(self):
        """start() must accept and store sample_rate without error."""
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
            interval_sec=60.0,
        )
        rpt.start(session_id="sr-test", sample_rate=44100)
        self.assertEqual(rpt._sample_rate, 44100)
        rpt.stop(timeout_sec=1.0)

    def test_cleanup_after_stop(self):
        """After stop(), is_running is False and _thread is None."""
        rpt = RealtimePartialTranscriber(
            transcriber=_make_transcriber(),
            recorder=_make_recorder(),
            event_bus=_make_bus(),
            interval_sec=60.0,
        )
        rpt.start(session_id="cleanup-test")
        rpt.stop(timeout_sec=2.0)
        self.assertFalse(rpt.is_running)
        self.assertIsNone(rpt._thread)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

class TestRealtimePartialConstants(unittest.TestCase):

    def test_event_type_strings(self):
        """Event type string constants must match expected values."""
        self.assertEqual(_REALTIME_PARTIAL_TYPE, "realtime.partial_transcript")
        self.assertEqual(_REALTIME_FINAL_TYPE, "realtime.final_transcript")


if __name__ == "__main__":
    unittest.main()
