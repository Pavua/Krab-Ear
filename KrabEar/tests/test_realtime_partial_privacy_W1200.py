"""Tests for privacy_mode gate in RealtimePartialTranscriber and
RecordingCoreService (W1200 — W1194 F1 MED fix).

Covers:
  - test_realtime_partial_not_started_in_privacy_mode
  - test_realtime_partial_skip_emit_when_privacy_toggled_mid_recording
  - test_realtime_partial_normal_emit_when_privacy_disabled
"""

from __future__ import annotations

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

# Path setup — needed when run standalone from repo root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_KRAB_EAR = os.path.join(_PROJECT_ROOT, "KrabEar")
if _KRAB_EAR not in sys.path:
    sys.path.insert(0, _KRAB_EAR)

import numpy as np

from backend.realtime_partial import RealtimePartialTranscriber, _REALTIME_PARTIAL_TYPE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audio(duration_sec: float = 3.0, sr: int = 16000) -> np.ndarray:
    return np.zeros(int(sr * duration_sec), dtype=np.float32)


def _make_recorder(initial_duration_sec: float = 3.0) -> MagicMock:
    """Return a recorder whose snapshot_audio increments duration each call.

    This ensures the ``(duration_sec - last_transcribed_duration) >= 0.5`` gate
    in the worker loop always passes, so every iteration produces a transcription.
    """
    recorder = MagicMock()
    recorder.sample_rate = 16000

    call_count = [0]

    def _snapshot(max_duration_sec: float = 8.0):
        call_count[0] += 1
        dur = initial_duration_sec + call_count[0] * 1.0
        audio = _make_audio(min(dur, max_duration_sec))
        return (audio, dur)

    recorder.snapshot_audio.side_effect = _snapshot
    return recorder


def _make_transcriber(text: str = "Привет мир") -> MagicMock:
    t = MagicMock()
    t.transcribe_preview.return_value = {"text": text}
    return t


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = MagicMock()
    return bus


def _make_settings_svc(privacy: bool = False) -> MagicMock:
    svc = MagicMock()
    svc.cached_settings.return_value = {
        "privacy_mode_enabled": privacy,
        "realtime_partial_enabled": True,
        "realtime_preview_enabled": False,
        "rt_partial_interval_sec": 0.05,
        "rt_partial_buffer_sec": 2.0,
        "quality_profile": "balanced",
    }
    return svc


def _make_recording_core_service(privacy: bool = False):  # noqa: F821
    """Import and wire a RecordingCoreService with minimal fakes."""
    from backend.recording_core_service import RecordingCoreService

    settings_svc = _make_settings_svc(privacy=privacy)
    recorder = _make_recorder()
    recorder.sample_rate = 16000
    # R2: Core читает is_recording ДО старта. У голого MagicMock любой
    # неуказанный атрибут истинен, что читалось бы как «микрофон уже занят»
    # и уводило старт в unmanaged_recording. Реальный AudioRecorder держит
    # False до start() и True после (recorder.py) — повторяем это.
    recorder.is_recording = False

    def _start(*_args, **_kwargs):
        recorder.is_recording = True
        return True

    recorder.start = MagicMock(side_effect=_start)

    svc = RecordingCoreService(
        recorder=recorder,
        transcriber=_make_transcriber(),
        translator=MagicMock(),
        store=MagicMock(),
        vocabulary=MagicMock(),
        settings_svc=settings_svc,
        llm_rewriter=MagicMock(),
        auto_glossary=MagicMock(),
        semantic_searcher=MagicMock(),
        context_memory=MagicMock(),
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=MagicMock(),
        action_items_extractor=MagicMock(),
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )
    return svc


# ---------------------------------------------------------------------------
# Tests: RealtimePartialTranscriber privacy_getter at emit layer
# ---------------------------------------------------------------------------

class TestRealtimePartialPrivacyGetter(unittest.TestCase):
    """Unit tests for the privacy_getter parameter added to RealtimePartialTranscriber."""

    def test_realtime_partial_normal_emit_when_privacy_disabled(self):
        """When privacy_getter returns False, partial transcript IS emitted."""
        recorder = _make_recorder(initial_duration_sec=3.0)
        transcriber = _make_transcriber("hello world")

        emitted_events: list = []
        emitted_signal = threading.Event()

        class _SpyBus:
            def emit(self, event_type, payload):
                emitted_events.append((event_type, payload))
                if event_type == _REALTIME_PARTIAL_TYPE:
                    emitted_signal.set()

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=_SpyBus(),
            interval_sec=0.05,
            buffer_sec=2.0,
            privacy_getter=lambda: False,  # privacy OFF
        )
        rpt.start(session_id="sess-normal")
        try:
            emitted_signal.wait(timeout=2.0)
        finally:
            rpt.stop(timeout_sec=1.0)

        # Emit must have been called with the partial event
        self.assertTrue(
            emitted_signal.is_set(),
            "Expected realtime.partial_transcript to be emitted when privacy_mode=False",
        )
        partial_calls = [e for e in emitted_events if e[0] == _REALTIME_PARTIAL_TYPE]
        self.assertGreater(len(partial_calls), 0)

    def test_realtime_partial_skip_emit_when_privacy_toggled_mid_recording(self):
        """privacy_getter returning True mid-recording suppresses emit.

        Sequence:
          1. Start with privacy=False → one emit is allowed.
          2. Toggle privacy=True → subsequent loop iterations skip emit.
        """
        recorder = _make_recorder(initial_duration_sec=3.0)
        transcriber = _make_transcriber("secret words")

        # Mutable flag to simulate mid-recording toggle
        privacy_state = [False]

        emitted_events: list = []
        first_allowed = threading.Event()

        class _SpyBus:
            def emit(self, event_type, payload):
                emitted_events.append((event_type, payload))
                if event_type == _REALTIME_PARTIAL_TYPE and not privacy_state[0]:
                    first_allowed.set()

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=_SpyBus(),
            interval_sec=0.05,
            buffer_sec=2.0,
            privacy_getter=lambda: privacy_state[0],
        )
        rpt.start(session_id="sess-toggle")

        try:
            # Wait for at least one emit with privacy OFF
            first_allowed.wait(timeout=2.0)
            self.assertTrue(first_allowed.is_set(), "First emit should occur before privacy toggle")

            # Count emits so far
            emit_count_before = len(
                [e for e in emitted_events if e[0] == _REALTIME_PARTIAL_TYPE]
            )

            # Toggle privacy ON
            privacy_state[0] = True

            # Wait several intervals; no new emits should appear
            time.sleep(0.3)

            emit_count_after = len(
                [e for e in emitted_events if e[0] == _REALTIME_PARTIAL_TYPE]
            )
        finally:
            rpt.stop(timeout_sec=1.0)

        # After toggle, no additional partial_transcript events should have been emitted
        self.assertEqual(
            emit_count_before,
            emit_count_after,
            f"Expected no new partial_transcript emits after privacy toggle "
            f"(before={emit_count_before}, after={emit_count_after})",
        )

    def test_privacy_getter_none_does_not_block_emit(self):
        """When privacy_getter is None (default), emits are not blocked."""
        recorder = _make_recorder(initial_duration_sec=3.0)
        transcriber = _make_transcriber("text without privacy")

        emitted_signal = threading.Event()

        class _SpyBus:
            def emit(self, event_type, payload):
                if event_type == _REALTIME_PARTIAL_TYPE:
                    emitted_signal.set()

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=_SpyBus(),
            interval_sec=0.05,
            buffer_sec=2.0,
            privacy_getter=None,
        )
        rpt.start(session_id="sess-no-getter")
        try:
            emitted_signal.wait(timeout=2.0)
        finally:
            rpt.stop(timeout_sec=1.0)

        self.assertTrue(emitted_signal.is_set(), "Emit should occur when privacy_getter=None")


# ---------------------------------------------------------------------------
# Tests: RecordingCoreService start guard
# ---------------------------------------------------------------------------

class TestRecordingCoreServicePrivacyStartGuard(unittest.TestCase):
    """Test that handle_start_recording does not launch RealtimePartialTranscriber
    when privacy_mode_enabled=True (W1200 start guard)."""

    def test_realtime_partial_not_started_in_privacy_mode(self):
        """When privacy_mode_enabled=True, _rt_partial must remain None after start."""
        svc = _make_recording_core_service(privacy=True)

        with patch("backend.recording_core_service.event_bus") as mock_bus:
            svc.handle_start_recording({})

        # RealtimePartialTranscriber must NOT have been started
        self.assertIsNone(
            svc._rt_partial,
            "Expected _rt_partial to be None when privacy_mode_enabled=True",
        )

    def test_realtime_partial_started_when_privacy_disabled(self):
        """When privacy_mode_enabled=False, _rt_partial is created and started."""
        svc = _make_recording_core_service(privacy=False)

        with patch("backend.recording_core_service.event_bus") as mock_bus, \
             patch("backend.recording_core_service.RealtimePartialTranscriber") as MockRPT:
            mock_instance = MagicMock()
            MockRPT.return_value = mock_instance

            svc.handle_start_recording({})

        # RealtimePartialTranscriber must have been instantiated and started
        MockRPT.assert_called_once()
        # Verify privacy_getter was passed
        call_kwargs = MockRPT.call_args.kwargs
        self.assertIn("privacy_getter", call_kwargs, "privacy_getter must be passed to constructor")
        self.assertIsNotNone(call_kwargs["privacy_getter"])
        # Callable should work
        privacy_fn = call_kwargs["privacy_getter"]
        self.assertFalse(privacy_fn(), "privacy_getter should return False initially")

    def test_privacy_getter_reflects_mid_recording_toggle(self):
        """privacy_getter closure reads from settings_svc.cached_settings each call."""
        from backend.recording_core_service import RecordingCoreService

        # Mutable settings backing store
        settings_dict = {
            "privacy_mode_enabled": False,
            "realtime_partial_enabled": True,
            "realtime_preview_enabled": False,
            "rt_partial_interval_sec": 0.05,
            "rt_partial_buffer_sec": 2.0,
            "quality_profile": "balanced",
        }

        settings_svc = MagicMock()
        settings_svc.cached_settings.side_effect = lambda: dict(settings_dict)

        recorder = MagicMock()
        recorder.sample_rate = 16000
        # См. комментарий в _make_recording_core_service: is_recording
        # обязан быть False до start(), иначе R2-Core читает чужую запись.
        recorder.is_recording = False

        def _start_recorder(*_args, **_kwargs):
            recorder.is_recording = True
            return True

        recorder.start = MagicMock(side_effect=_start_recorder)

        svc = RecordingCoreService(
            recorder=recorder,
            transcriber=_make_transcriber(),
            translator=MagicMock(),
            store=MagicMock(),
            vocabulary=MagicMock(),
            settings_svc=settings_svc,
            llm_rewriter=MagicMock(),
            auto_glossary=MagicMock(),
            semantic_searcher=MagicMock(),
            context_memory=MagicMock(),
            clipboard_history=[],
            auto_backup=MagicMock(),
            session_tracker=MagicMock(),
            action_items_extractor=MagicMock(),
            transcription_counter_ref=[0],
            last_stt_engine_ref=[None],
        )

        captured_getter: list = []

        with patch("backend.recording_core_service.event_bus"), \
             patch("backend.recording_core_service.RealtimePartialTranscriber") as MockRPT:
            def capture_constructor(**kwargs):
                captured_getter.append(kwargs.get("privacy_getter"))
                inst = MagicMock()
                inst.start = MagicMock()
                return inst
            MockRPT.side_effect = capture_constructor
            svc.handle_start_recording({})

        self.assertTrue(len(captured_getter) > 0, "Constructor should have been called")
        getter = captured_getter[0]
        self.assertIsNotNone(getter)

        # Initially False
        self.assertFalse(getter())

        # Toggle privacy ON mid-recording
        settings_dict["privacy_mode_enabled"] = True
        self.assertTrue(getter(), "getter should reflect runtime settings change")

        # Toggle back OFF
        settings_dict["privacy_mode_enabled"] = False
        self.assertFalse(getter(), "getter should reflect settings toggle back to False")


if __name__ == "__main__":
    unittest.main()
