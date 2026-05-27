"""Tests for W1390 fix: stale _last_silence_ranges cleared at recording start.

Covers W1385 F1 MED — when RSF is toggled off between recordings, leftover
timestamps from the prior recording must NOT be forwarded to the new STT call.

Tests:
  - test_start_recording_clears_stale_silence_ranges
  - test_rsf_toggled_off_no_silence_applied_to_new_recording
  - test_rsf_re_enabled_starts_fresh
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure backend.* and core.* are importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Fakes (mirror test_rsf_wiring_W1329.py for test isolation)
# ---------------------------------------------------------------------------

class FakeRecorder:
    is_recording = True
    sample_rate = 16000

    def start(self):
        return True

    def stop(self, trim_tail_ms=0):
        import numpy as np
        audio = np.ones(16000, dtype=np.float32) * 0.1
        return audio, 1.0

    def snapshot_audio(self, max_duration_sec=None):
        import numpy as np
        return np.ones(16000, dtype=np.float32) * 0.1, 1.0

    def snapshot_rms(self):
        return 0.1


class FakeTranscriber:
    """Captures the call to transcribe() for assertion."""

    def __init__(self):
        self.last_call_kwargs: dict = {}

    def transcribe(self, audio, **kwargs):
        self.last_call_kwargs = kwargs
        return {"text": "test transcript", "confidence": 0.9}

    def transcribe_preview(self, audio, **kwargs):
        return {"text": "preview"}


class FakeTranslator:
    def translate(self, text, **kwargs):
        result = MagicMock()
        result.ok = False
        result.text = ""
        result.status = "not_requested"
        result.mode = "off"
        result.source_lang = None
        result.target_lang = None
        result.engine = None
        return result


class FakeStore:
    data_dir = Path("/tmp")

    def add_history_item(self, **kwargs):
        item = MagicMock()
        item.id = "test-id-1"
        item.ts = "2026-01-01T00:00:00"
        return item

    def get_history_page(self, cursor=None, limit=10):
        return [], None


class FakeVocabulary:
    def load(self):
        return []


class FakeSettingsSvc:
    def __init__(self, settings: dict | None = None):
        self._settings = settings or {}

    def cached_settings(self):
        return dict(self._settings)


class FakeLLMRewriter:
    pass


class FakeAutoGlossary:
    def build(self, **kwargs):
        return []


class FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, *a, **kw):
        pass


class FakeContextMemory:
    def update(self, *a):
        pass


class FakeAutoBackup:
    def check_and_backup(self):
        pass


class FakeSessionTracker:
    _active_session = None


class FakeActionItemsExtractor:
    pass


# ---------------------------------------------------------------------------
# Helper: build a RecordingCoreService with controllable settings
# ---------------------------------------------------------------------------

def _make_service(settings: dict | None = None, transcriber=None, recorder=None):
    from backend.recording_core_service import RecordingCoreService

    _settings = settings or {}
    _transcriber = transcriber or FakeTranscriber()
    _recorder = recorder or FakeRecorder()
    svc = RecordingCoreService(
        recorder=_recorder,
        transcriber=_transcriber,
        translator=FakeTranslator(),
        store=FakeStore(),
        vocabulary=FakeVocabulary(),
        settings_svc=FakeSettingsSvc(_settings),
        llm_rewriter=None,
        auto_glossary=FakeAutoGlossary(),
        semantic_searcher=FakeSemanticSearcher(),
        context_memory=FakeContextMemory(),
        clipboard_history=[],
        auto_backup=FakeAutoBackup(),
        session_tracker=FakeSessionTracker(),
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )
    return svc, _transcriber, _recorder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRSFStaleRangesW1390(unittest.TestCase):
    """W1390 / W1385 F1 MED: stale silence_ranges cleared at recording start."""

    # ------------------------------------------------------------------
    # Layer 1: handle_start_recording always resets state
    # ------------------------------------------------------------------

    def test_start_recording_clears_stale_silence_ranges(self):
        """handle_start_recording must set _last_silence_ranges = [] and _rsf = None
        even before the conditional RSF-enabled block runs."""
        settings = {"realtime_silence_filter_enabled": False}
        svc, _, _ = _make_service(settings=settings)

        # Inject stale state as if a previous recording with RSF enabled left ranges
        svc._last_silence_ranges = [(0.5, 2.0), (3.0, 4.5)]
        svc._rsf = MagicMock()  # stale non-None instance

        svc.handle_start_recording({})

        # After start, stale state must be cleared regardless of RSF enabled flag
        self.assertEqual(
            svc._last_silence_ranges, [],
            "handle_start_recording must clear _last_silence_ranges unconditionally",
        )
        self.assertIsNone(
            svc._rsf,
            "handle_start_recording must set _rsf = None unconditionally",
        )

    def test_start_recording_clears_state_even_when_rsf_enabled(self):
        """Same guarantee when RSF is enabled this recording — start must still reset
        the stale ranges before instantiating a fresh RSF."""
        settings = {"realtime_silence_filter_enabled": True}
        fake_transcriber = FakeTranscriber()
        svc, _, _ = _make_service(settings=settings, transcriber=fake_transcriber)

        # Inject stale ranges
        stale_ranges = [(10.0, 11.0)]
        svc._last_silence_ranges = list(stale_ranges)

        mock_rsf = MagicMock()
        mock_rsf.stop.return_value = []  # new recording: no silences

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter",
            return_value=mock_rsf,
        ):
            svc.handle_start_recording({})
            # After start(), _last_silence_ranges should be [] (cleared), not the stale list
            self.assertEqual(svc._last_silence_ranges, [])

    # ------------------------------------------------------------------
    # Layer 2: RSF toggled off — stale ranges must NOT reach transcribe()
    # ------------------------------------------------------------------

    def test_rsf_toggled_off_no_silence_applied_to_new_recording(self):
        """Recording 1 with RSF ON deposits ranges.
        Recording 2 with RSF OFF must not forward those ranges to transcribe()."""
        fake_transcriber = FakeTranscriber()

        # --- Recording 1: RSF enabled ---
        settings_rsf_on = {"realtime_silence_filter_enabled": True}
        svc, _, _ = _make_service(settings=settings_rsf_on, transcriber=fake_transcriber)

        stale_ranges = [(1.0, 3.0), (5.0, 7.0)]
        mock_rsf = MagicMock()
        mock_rsf.stop.return_value = stale_ranges

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter",
            return_value=mock_rsf,
        ):
            svc.handle_start_recording({})
            svc.handle_stop_recording({})

        # Verify ranges were forwarded for recording 1 (baseline check)
        self.assertEqual(
            fake_transcriber.last_call_kwargs.get("silence_ranges"),
            stale_ranges,
            "Recording 1 should have forwarded RSF ranges",
        )

        # --- Recording 2: RSF disabled ---
        # Simulate settings toggle: update the internal settings dict
        svc._settings_svc._settings = {"realtime_silence_filter_enabled": False}

        # Verify stale _last_silence_ranges is still on the instance pre-start
        self.assertEqual(svc._last_silence_ranges, stale_ranges)

        # Now start + stop recording 2
        svc.handle_start_recording({})
        svc.handle_stop_recording({})

        sr_after = fake_transcriber.last_call_kwargs.get("silence_ranges")
        self.assertFalse(
            bool(sr_after),
            f"Recording 2 (RSF OFF) must NOT forward stale ranges, got: {sr_after!r}",
        )

    # ------------------------------------------------------------------
    # Layer 3: RSF re-enabled after toggling off — gets a fresh empty start
    # ------------------------------------------------------------------

    def test_rsf_re_enabled_starts_fresh(self):
        """After RSF OFF recording, re-enable RSF for the next recording.
        The new recording's transcription should only receive ranges from the
        freshly-started RSF, not any stale leftovers."""
        fake_transcriber = FakeTranscriber()

        # --- Recording 1: RSF OFF ---
        settings_rsf_off = {"realtime_silence_filter_enabled": False}
        svc, _, _ = _make_service(settings=settings_rsf_off, transcriber=fake_transcriber)

        svc.handle_start_recording({})
        svc.handle_stop_recording({})

        # _last_silence_ranges must be [] after a no-RSF recording
        self.assertEqual(svc._last_silence_ranges, [])

        # --- Recording 2: RSF re-enabled ---
        fresh_ranges = [(2.0, 4.0)]
        svc._settings_svc._settings = {"realtime_silence_filter_enabled": True}

        mock_rsf2 = MagicMock()
        mock_rsf2.stop.return_value = fresh_ranges

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter",
            return_value=mock_rsf2,
        ):
            svc.handle_start_recording({})
            # At this point _last_silence_ranges must be [] (cleared at start)
            self.assertEqual(
                svc._last_silence_ranges, [],
                "_last_silence_ranges must be cleared at start even when RSF will be re-enabled",
            )
            svc.handle_stop_recording({})

        # The transcriber for recording 2 should receive only the fresh ranges
        sr = fake_transcriber.last_call_kwargs.get("silence_ranges")
        self.assertEqual(
            sr, fresh_ranges,
            f"Recording 2 (RSF re-enabled) should forward only the new RSF ranges, got: {sr!r}",
        )

    # ------------------------------------------------------------------
    # Additional: _last_silence_ranges starts empty on fresh instance
    # ------------------------------------------------------------------

    def test_initial_state_has_empty_silence_ranges(self):
        """A freshly constructed RecordingCoreService has _last_silence_ranges = []."""
        svc, _, _ = _make_service()
        self.assertEqual(svc._last_silence_ranges, [])
        self.assertIsNone(svc._rsf)

    def test_defensive_rsf_disabled_forces_none_silence_ranges(self):
        """When RSF is disabled in settings at transcribe time, _stop_recording_phase_c
        forces silence_ranges=None regardless of what _last_silence_ranges holds."""
        fake_transcriber = FakeTranscriber()
        settings = {"realtime_silence_filter_enabled": False}
        svc, _, _ = _make_service(settings=settings, transcriber=fake_transcriber)

        # Manually inject stale ranges to test the defensive layer in phase_c
        svc._last_silence_ranges = [(99.0, 100.0)]

        svc.handle_start_recording({})  # <-- clears _last_silence_ranges to []
        # After start, stale ranges are gone; but simulate an edge case:
        # inject them again AFTER start (as if phase_a didn't clear them)
        svc._last_silence_ranges = [(99.0, 100.0)]

        svc.handle_stop_recording({})

        sr = fake_transcriber.last_call_kwargs.get("silence_ranges")
        self.assertFalse(
            bool(sr),
            f"Defensive guard must force silence_ranges=None when RSF is disabled, got: {sr!r}",
        )


if __name__ == "__main__":
    unittest.main()
