"""Tests for W1329 fix: RealtimeSilenceFilter wired into RecordingCoreService.

Covers:
  - test_rsf_started_when_enabled
  - test_rsf_not_started_when_disabled
  - test_silence_ranges_passed_to_transcribe
"""

from __future__ import annotations

import sys
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Ensure backend.* and core.* are importable

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.test_helpers import make_test_item  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRecorder:
    # R2: Core читает is_recording ДО старта, чтобы отличить свою запись от
    # чужой. Реальный AudioRecorder держит False до start() (recorder.py),
    # поэтому константа True здесь означала бы «микрофон уже занят кем-то».
    sample_rate = 16000

    def __init__(self):
        self.is_recording = False

    def start(self):
        self.is_recording = True
        return True

    def stop(self, trim_tail_ms=0):
        import numpy as np
        self.is_recording = False
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
        self.last_call_kwargs = {}

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
        return make_test_item(id="test-id-1", ts="2026-01-01T00:00:00")

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
# Helper to build a RecordingCoreService
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

class TestRSFWiringW1329(unittest.TestCase):
    """W1329: RealtimeSilenceFilter is instantiated and started when enabled."""

    def test_rsf_started_when_enabled(self):
        """When realtime_silence_filter_enabled=True, RSF is instantiated and started."""
        settings = {"realtime_silence_filter_enabled": True}
        svc, _, _ = _make_service(settings=settings)

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter"
        ) as MockRSF:
            mock_instance = MagicMock()
            MockRSF.return_value = mock_instance

            svc.handle_start_recording({})

            # RSF should have been constructed
            MockRSF.assert_called_once()
            # Constructor should have received the recorder
            ctor_kwargs = MockRSF.call_args
            self.assertIsNotNone(ctor_kwargs)
            # start() should have been called
            mock_instance.start.assert_called_once()

    def test_rsf_not_started_when_disabled(self):
        """When realtime_silence_filter_enabled is absent/False, RSF is NOT instantiated."""
        settings = {"realtime_silence_filter_enabled": False}
        svc, _, _ = _make_service(settings=settings)

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter"
        ) as MockRSF:
            svc.handle_start_recording({})

            MockRSF.assert_not_called()

    def test_rsf_not_started_by_default(self):
        """When realtime_silence_filter_enabled is not in settings, RSF is NOT started."""
        svc, _, _ = _make_service(settings={})  # no key at all

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter"
        ) as MockRSF:
            svc.handle_start_recording({})

            MockRSF.assert_not_called()

    def test_silence_ranges_passed_to_transcribe(self):
        """Silence ranges collected from RSF.stop() are forwarded to transcriber.transcribe()."""
        settings = {"realtime_silence_filter_enabled": True}
        fake_transcriber = FakeTranscriber()
        svc, _, recorder = _make_service(
            settings=settings, transcriber=fake_transcriber
        )

        fake_ranges = [(1.0, 3.0), (5.5, 7.0)]

        mock_rsf_instance = MagicMock()
        mock_rsf_instance.stop.return_value = fake_ranges

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter",
            return_value=mock_rsf_instance,
        ):
            # Start recording (wires RSF)
            svc.handle_start_recording({})

            # Stop recording (should capture ranges and pass to transcribe)
            svc.handle_stop_recording({})

        # The transcriber should have received the silence_ranges
        self.assertIn("silence_ranges", fake_transcriber.last_call_kwargs)
        self.assertEqual(
            fake_transcriber.last_call_kwargs["silence_ranges"],
            fake_ranges,
        )

    def test_silence_ranges_none_when_rsf_disabled(self):
        """When RSF is disabled, silence_ranges passed to transcribe is None (no arg)."""
        settings = {"realtime_silence_filter_enabled": False}
        fake_transcriber = FakeTranscriber()
        svc, _, _ = _make_service(settings=settings, transcriber=fake_transcriber)

        svc.handle_start_recording({})
        svc.handle_stop_recording({})

        # silence_ranges kwarg should be None or absent (falsy) when no RSF ran
        sr = fake_transcriber.last_call_kwargs.get("silence_ranges")
        self.assertFalse(bool(sr), f"Expected empty/None silence_ranges, got: {sr!r}")

    def test_rsf_start_failure_does_not_break_recording(self):
        """If RSF instantiation raises, recording still starts normally."""
        settings = {"realtime_silence_filter_enabled": True}
        svc, _, _ = _make_service(settings=settings)

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter",
            side_effect=RuntimeError("RSF init failed"),
        ):
            result = svc.handle_start_recording({})

        self.assertEqual(result["status"], "recording")
        self.assertIsNone(svc._rsf)

    def test_rsf_stop_failure_does_not_break_stop_recording(self):
        """If RSF.stop() raises, stop_recording still completes normally."""
        settings = {"realtime_silence_filter_enabled": True}
        fake_transcriber = FakeTranscriber()
        svc, _, _ = _make_service(settings=settings, transcriber=fake_transcriber)

        mock_rsf_instance = MagicMock()
        mock_rsf_instance.stop.side_effect = RuntimeError("RSF stop exploded")

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter",
            return_value=mock_rsf_instance,
        ):
            svc.handle_start_recording({})
            result = svc.handle_stop_recording({})

        # Recording should have completed without error
        self.assertIn(result.get("status"), ("ok", "empty_text", "empty_audio"))
        # Runtime-hardening 2026-07-20: упавший stop() значит «фильтр, возможно,
        # ещё жив» — хэндл ВОЗВРАЩАЕТСЯ в слот (symметрично recorder'у), а не
        # теряется. Главный инвариант теста неизменен: stop_recording не падает.
        self.assertIs(svc._rsf, mock_rsf_instance)

    def test_rsf_instance_cleared_after_stop(self):
        """After stop_recording, _rsf is None regardless of enabled state."""
        settings = {"realtime_silence_filter_enabled": True}
        svc, _, _ = _make_service(settings=settings)

        mock_rsf_instance = MagicMock()
        mock_rsf_instance.stop.return_value = []
        # MagicMock.is_running был бы truthy-моком → phase_a сочла бы фильтр
        # «не остановившимся» и вернула его в слот (hardening 2026-07-20).
        mock_rsf_instance.is_running = False

        with patch(
            "backend.recording_core_service.RealtimeSilenceFilter",
            return_value=mock_rsf_instance,
        ):
            svc.handle_start_recording({})
            self.assertIsNotNone(svc._rsf)  # RSF is set after start

            svc.handle_stop_recording({})

        self.assertIsNone(svc._rsf)  # RSF cleared after stop


if __name__ == "__main__":
    unittest.main()
