"""W1139 — tests for RSF silence_ranges wiring through the transcribe pipeline.

Verifies W1136 F3 MED fix:
  - test_silence_ranges_passed_to_transcribe: RSF silence_ranges flow to transcriber
  - test_silence_ranges_none_default: no RSF → silence_ranges=None → old behavior preserved
  - test_silence_ranges_excluded_from_output: engine zeroes audio at silence_ranges
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Fakes / stubs
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16000

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        audio = np.ones(16000, dtype=np.float32) * 0.1
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self, max_duration_sec=12.0):
        audio = np.ones(16000, dtype=np.float32) * 0.1
        return audio, 1.0


class _CapturingTranscriber:
    """Records the kwargs that transcribe() was called with."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio": audio, **kwargs})
        return {"text": "hello world", "confidence": 0.9, "engine": "fake"}


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text,
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
        )


class _FakeSemanticSearcher:
    is_enabled = False

    def add(self, *a, **kw):
        pass


class _FakeSettingsSvc:
    _settings = {
        "quality_profile": "balanced",
        "cleanup_profile": "soft",
        "silence_guard_enabled": False,
        "background_guard_enabled": False,
        "realtime_preview_enabled": False,
        "realtime_partial_enabled": False,
        "realtime_silence_filter_enabled": False,
        "translate_and_paste": False,
        "translation_mode": "off",
        "auto_glossary_enabled": False,
        "stt_hotwords_enabled": False,
        "stt_hotwords": [],
        "diarization_enabled": False,
    }

    def cached_settings(self):
        return dict(self._settings)


class _RSFEnabledSettingsSvc(_FakeSettingsSvc):
    _settings = dict(_FakeSettingsSvc._settings)
    _settings["realtime_silence_filter_enabled"] = True


def _make_service(tmp_dir, recorder=None, transcriber=None, settings_svc=None):
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.load.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None

    return RecordingCoreService(
        recorder=recorder or _FakeRecorder(),
        transcriber=transcriber or _CapturingTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=settings_svc or _FakeSettingsSvc(),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=session_tracker,
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSilenceRangesNoneDefault(unittest.TestCase):
    """W1139 — with RSF disabled, silence_ranges=None is passed to transcribe."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_silence_ranges_none_default(self):
        """When RSF is disabled (default), silence_ranges=None preserves old behavior."""
        tr = _CapturingTranscriber()
        svc = _make_service(self._tmp, transcriber=tr)

        svc.handle_start_recording({})
        svc.handle_stop_recording({})

        self.assertGreater(len(tr.calls), 0, "transcribe() should have been called")
        call_kwargs = tr.calls[-1]
        # silence_ranges should be None (not passed or explicitly None)
        self.assertIsNone(call_kwargs.get("silence_ranges"),
                          "silence_ranges must be None when RSF is disabled")

    def test_rsf_not_started_when_disabled(self):
        """No RSF instance created when realtime_silence_filter_enabled=False."""
        svc = _make_service(self._tmp)
        svc.handle_start_recording({})
        # RSF is disabled by default settings → no filter instance
        self.assertIsNone(svc._rsf,
                          "RSF should not be running when disabled")
        svc.handle_stop_recording({})


class TestSilenceRangesPassedToTranscribe(unittest.TestCase):
    """W1139 — RSF silence_ranges are passed to transcribe() when filter is enabled."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_silence_ranges_passed_to_transcribe(self):
        """When RSF returns ranges, they reach transcriber.transcribe()."""
        fake_ranges = [(0.5, 2.0), (4.0, 5.5)]

        tr = _CapturingTranscriber()
        recorder = _FakeRecorder()

        with patch("backend.recording_core_service.RealtimeSilenceFilter") as MockRSF:
            mock_filter = MagicMock()
            mock_filter.is_running = False  # hardening 2026-07-20: truthy-мок вернул бы фильтр в слот
            mock_filter.enabled = True
            mock_filter.stop.return_value = fake_ranges
            MockRSF.return_value = mock_filter

            svc = _make_service(self._tmp, recorder=recorder, transcriber=tr,
                                settings_svc=_RSFEnabledSettingsSvc())

            svc.handle_start_recording({})
            mock_filter.start.assert_called_once()

            svc.handle_stop_recording({})
            mock_filter.stop.assert_called_once()

        self.assertGreater(len(tr.calls), 0, "transcribe() should have been called")
        call_kwargs = tr.calls[-1]
        self.assertEqual(call_kwargs.get("silence_ranges"), fake_ranges,
                         "silence_ranges from RSF must reach transcribe()")

    def test_rsf_started_when_enabled(self):
        """RSF.start() is called on handle_start_recording when enabled."""
        recorder = _FakeRecorder()

        with patch("backend.recording_core_service.RealtimeSilenceFilter") as MockRSF:
            mock_filter = MagicMock()
            mock_filter.is_running = False  # hardening 2026-07-20: truthy-мок вернул бы фильтр в слот
            mock_filter.enabled = True
            mock_filter.stop.return_value = []
            MockRSF.return_value = mock_filter

            svc = _make_service(self._tmp, recorder=recorder,
                                settings_svc=_RSFEnabledSettingsSvc())
            svc.handle_start_recording({})

            MockRSF.assert_called_once()
            mock_filter.start.assert_called_once()
            svc.handle_stop_recording({})

    def test_rsf_cleaned_up_after_stop(self):
        """_realtime_silence_filter is set to None after stop_recording."""
        recorder = _FakeRecorder()

        with patch("backend.recording_core_service.RealtimeSilenceFilter") as MockRSF:
            mock_filter = MagicMock()
            mock_filter.is_running = False  # hardening 2026-07-20: truthy-мок вернул бы фильтр в слот
            mock_filter.stop.return_value = []
            MockRSF.return_value = mock_filter

            svc = _make_service(self._tmp, recorder=recorder,
                                settings_svc=_RSFEnabledSettingsSvc())
            svc.handle_start_recording({})
            svc.handle_stop_recording({})

        self.assertIsNone(svc._rsf,
                          "_realtime_silence_filter must be None after stop")

    def test_empty_silence_ranges_passed_as_none(self):
        """Empty RSF ranges result in silence_ranges=None to transcribe()."""
        tr = _CapturingTranscriber()
        recorder = _FakeRecorder()

        with patch("backend.recording_core_service.RealtimeSilenceFilter") as MockRSF:
            mock_filter = MagicMock()
            mock_filter.is_running = False  # hardening 2026-07-20: truthy-мок вернул бы фильтр в слот
            mock_filter.stop.return_value = []
            MockRSF.return_value = mock_filter

            svc = _make_service(self._tmp, recorder=recorder, transcriber=tr,
                                settings_svc=_RSFEnabledSettingsSvc())
            svc.handle_start_recording({})
            svc.handle_stop_recording({})

        self.assertGreater(len(tr.calls), 0)
        call_kwargs = tr.calls[-1]
        # Empty list → falsy → passed as None
        self.assertIsNone(call_kwargs.get("silence_ranges"),
                          "Empty silence_ranges list should be passed as None")


class TestSilenceRangesExcludedFromOutput(unittest.TestCase):
    """W1139 — AudioEngine.zero_silence_ranges zeroes silence in audio before STT."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_silence_ranges_excluded_from_output(self):
        """zero_silence_ranges() zeroes the correct audio samples for given ranges."""
        from backend.realtime_silence_filter import zero_silence_ranges

        audio = np.ones(32000, dtype=np.float32)  # 2s of audio at 16kHz
        silence_ranges = [(0.5, 1.0)]  # 0.5s → 1.0s should be zeroed

        result = zero_silence_ranges(audio, silence_ranges, sample_rate=16000)

        # Samples before silence should be unmodified
        self.assertTrue(np.all(result[:8000] == 1.0),
                        "Pre-silence samples must be unmodified")
        # Samples within silence range should be 0
        self.assertTrue(np.all(result[8000:16000] == 0.0),
                        "Silence range samples must be zeroed")
        # Samples after silence should be unmodified
        self.assertTrue(np.all(result[16000:] == 1.0),
                        "Post-silence samples must be unmodified")

    def test_zero_silence_ranges_none_or_empty_is_noop(self):
        """zero_silence_ranges with None or empty list returns audio unchanged."""
        from backend.realtime_silence_filter import zero_silence_ranges

        audio = np.ones(16000, dtype=np.float32)

        result_none = zero_silence_ranges(audio, [], sample_rate=16000)
        self.assertIs(result_none, audio, "Empty ranges must return original array")

    def test_zero_silence_ranges_preserves_shape(self):
        """zero_silence_ranges returns array with same shape."""
        from backend.realtime_silence_filter import zero_silence_ranges

        audio = np.ones(24000, dtype=np.float32)
        ranges = [(0.1, 0.3), (0.8, 1.0)]
        result = zero_silence_ranges(audio, ranges, sample_rate=16000)
        self.assertEqual(result.shape, audio.shape)


class TestTranscriberSilenceRangesParam(unittest.TestCase):
    """W1139 — Transcriber.transcribe() accepts and forwards silence_ranges."""

    def test_silence_ranges_forwarded_to_engine(self):
        """Transcriber passes silence_ranges to engine.transcribe()."""
        from backend.transcriber import Transcriber

        mock_engine = MagicMock()
        mock_engine.transcribe.return_value = {"text": "ok", "confidence": 0.9}

        tr = Transcriber(engine=mock_engine)
        fake_ranges = [(1.0, 2.0)]
        tr.transcribe(
            np.zeros(16000, dtype=np.float32),
            silence_ranges=fake_ranges,
        )

        call_kwargs = mock_engine.transcribe.call_args[1]
        self.assertEqual(call_kwargs.get("silence_ranges"), fake_ranges,
                         "silence_ranges must be forwarded to engine.transcribe()")

    def test_silence_ranges_none_default(self):
        """Transcriber.transcribe() defaults to silence_ranges=None."""
        from backend.transcriber import Transcriber

        mock_engine = MagicMock()
        mock_engine.transcribe.return_value = {"text": "ok", "confidence": 0.9}

        tr = Transcriber(engine=mock_engine)
        tr.transcribe(np.zeros(16000, dtype=np.float32))

        call_kwargs = mock_engine.transcribe.call_args[1]
        self.assertIsNone(call_kwargs.get("silence_ranges"),
                          "Default silence_ranges must be None")


if __name__ == "__main__":
    unittest.main()
