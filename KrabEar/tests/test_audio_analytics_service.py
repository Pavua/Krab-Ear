"""Unit tests — AudioAnalyticsService (8 IPC handlers).

Tests each handler directly against mocked collaborators, then an integration
smoke-test exercises them via BackendService.handle_request.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_analytics_service import AudioAnalyticsService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(
    audio_converter=None,
    quality_trends=None,
    audio_fingerprinter=None,
    word_timing_analyzer=None,
    store=None,
) -> AudioAnalyticsService:
    return AudioAnalyticsService(
        audio_converter=audio_converter or MagicMock(),
        quality_trends=quality_trends or MagicMock(),
        audio_fingerprinter=audio_fingerprinter or MagicMock(),
        word_timing_analyzer=word_timing_analyzer or MagicMock(),
        store=store or MagicMock(),
    )


class _FakeStore:
    """Minimal fake StateStore for AudioAnalyticsService tests."""

    def __init__(self, items=None):
        self._items = items or []

    class _CM:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _lock(self):
        return self._CM()

    def _load_active_items_unlocked(self):
        return self._items


def _fake_store_with_items(items=None):
    return _FakeStore(items=items)


# ---------------------------------------------------------------------------
# handle_analyze_audio_quality
# ---------------------------------------------------------------------------

class TestAnalyzeAudioQuality(unittest.TestCase):
    def test_missing_file_path_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_analyze_audio_quality({})

    def test_calls_analyze_file_and_returns_dict(self) -> None:
        fake_report = MagicMock()
        fake_report.to_dict.return_value = {"quality_score": 85, "warnings": []}

        svc = _make_service()
        with patch("core.audio_quality.analyze_file", return_value=fake_report) as mock_af:
            result = svc.handle_analyze_audio_quality({"file_path": "/tmp/audio.wav"})

        mock_af.assert_called_once_with("/tmp/audio.wav")
        self.assertEqual(result["quality_score"], 85)
        self.assertEqual(result["warnings"], [])


# ---------------------------------------------------------------------------
# handle_analyze_silence
# ---------------------------------------------------------------------------

class TestAnalyzeSilence(unittest.TestCase):
    def test_missing_file_path_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_analyze_silence({})

    def test_default_threshold(self) -> None:
        fake_result = {"speech_ratio": 0.8, "total_silence_sec": 2.0}
        svc = _make_service()
        with patch("core.silence_detector.analyze_silence_file", return_value=fake_result) as mock_fn:
            result = svc.handle_analyze_silence({"file_path": "/tmp/audio.wav"})
            mock_fn.assert_called_once_with("/tmp/audio.wav", threshold_db=-40.0)
        self.assertEqual(result["speech_ratio"], 0.8)

    def test_custom_threshold(self) -> None:
        fake_result = {"speech_ratio": 0.9, "total_silence_sec": 1.0}
        svc = _make_service()
        with patch("core.silence_detector.analyze_silence_file", return_value=fake_result) as mock_fn:
            svc.handle_analyze_silence({"file_path": "/tmp/a.wav", "threshold_db": -30.0})
            mock_fn.assert_called_once_with("/tmp/a.wav", threshold_db=-30.0)


# ---------------------------------------------------------------------------
# handle_analyze_quality_trends
# ---------------------------------------------------------------------------

class TestAnalyzeQualityTrends(unittest.TestCase):
    def test_returns_trend_report(self) -> None:
        qt = MagicMock()
        report = MagicMock()
        report.daily_confidence = {"2026-05-18": 0.92}
        report.overall_trend = "improving"
        report.trend_slope = 0.002
        report.best_day = "2026-05-18"
        report.worst_day = "2026-05-10"
        report.confidence_distribution = {"high": 20, "medium": 5, "low": 1}
        qt.analyze_trends.return_value = report

        store = _fake_store_with_items([])
        svc = _make_service(quality_trends=qt, store=store)
        result = svc.handle_analyze_quality_trends({"days": 7})

        qt.analyze_trends.assert_called_once_with([], days=7)
        self.assertEqual(result["overall_trend"], "improving")
        self.assertIn("daily_confidence", result)

    def test_default_days_30(self) -> None:
        qt = MagicMock()
        report = MagicMock()
        report.daily_confidence = {}
        report.overall_trend = "stable"
        report.trend_slope = 0.0
        report.best_day = None
        report.worst_day = None
        report.confidence_distribution = {}
        qt.analyze_trends.return_value = report

        store = _fake_store_with_items([])
        svc = _make_service(quality_trends=qt, store=store)
        svc.handle_analyze_quality_trends({})
        qt.analyze_trends.assert_called_once_with([], days=30)

    def test_store_error_falls_back_to_empty(self) -> None:
        qt = MagicMock()
        report = MagicMock()
        report.daily_confidence = {}
        report.overall_trend = "stable"
        report.trend_slope = 0.0
        report.best_day = None
        report.worst_day = None
        report.confidence_distribution = {}
        qt.analyze_trends.return_value = report

        class _ErrorStore:
            def _lock(self):
                raise Exception("lock fail")

        svc = _make_service(quality_trends=qt, store=_ErrorStore())
        # should not raise — falls back to items=[]
        result = svc.handle_analyze_quality_trends({"days": 14})
        qt.analyze_trends.assert_called_once_with([], days=14)
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# handle_analyze_word_timing
# ---------------------------------------------------------------------------

class TestAnalyzeWordTiming(unittest.TestCase):
    def test_missing_segments_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_analyze_word_timing({"segments": "not a list"})

    def test_non_list_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_analyze_word_timing({})

    def test_ok_delegates_to_analyzer(self) -> None:
        wta = MagicMock()
        report = MagicMock()
        report.as_dict.return_value = {"avg_wpm": 120.0, "pauses": []}
        wta.analyze.return_value = report

        svc = _make_service(word_timing_analyzer=wta)
        segments = [{"start": 0.0, "end": 1.0, "text": "hello"}]
        result = svc.handle_analyze_word_timing({"segments": segments})

        wta.analyze.assert_called_once_with(segments)
        self.assertEqual(result["avg_wpm"], 120.0)


# ---------------------------------------------------------------------------
# handle_get_audio_info
# ---------------------------------------------------------------------------

class TestGetAudioInfo(unittest.TestCase):
    def test_missing_path_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_get_audio_info({})

    def test_returns_info_dict(self) -> None:
        conv = MagicMock()
        info = MagicMock()
        info.duration = 120.5
        info.sample_rate = 44100
        info.channels = 2
        info.format = "flac"
        info.size_mb = 5.2
        conv.get_audio_info.return_value = info

        svc = _make_service(audio_converter=conv)
        result = svc.handle_get_audio_info({"path": "/tmp/audio.flac"})

        conv.get_audio_info.assert_called_once_with("/tmp/audio.flac")
        self.assertEqual(result["duration"], 120.5)
        self.assertEqual(result["format"], "flac")
        self.assertEqual(result["channels"], 2)


# ---------------------------------------------------------------------------
# handle_get_waveform
# ---------------------------------------------------------------------------

class TestGetWaveform(unittest.TestCase):
    def test_missing_file_path_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_get_waveform({})

    def test_default_num_points(self) -> None:
        fake_wf = MagicMock()
        fake_wf.points = [0.1] * 200
        fake_wf.duration_sec = 10.0
        fake_wf.sample_rate = 16000
        fake_wf.peak_amplitude = 0.95
        fake_wf.rms_amplitude = 0.4

        fake_gen = MagicMock()
        fake_gen.generate_from_file.return_value = fake_wf

        svc = _make_service()
        with patch("core.waveform_generator.WaveformGenerator", return_value=fake_gen):
            result = svc.handle_get_waveform({"file_path": "/tmp/audio.wav"})

        fake_gen.generate_from_file.assert_called_once_with("/tmp/audio.wav", num_points=200)
        self.assertEqual(len(result["points"]), 200)
        self.assertEqual(result["sample_rate"], 16000)

    def test_custom_num_points(self) -> None:
        fake_wf = MagicMock()
        fake_wf.points = [0.0] * 100
        fake_wf.duration_sec = 5.0
        fake_wf.sample_rate = 16000
        fake_wf.peak_amplitude = 0.5
        fake_wf.rms_amplitude = 0.2

        fake_gen = MagicMock()
        fake_gen.generate_from_file.return_value = fake_wf

        svc = _make_service()
        with patch("core.waveform_generator.WaveformGenerator", return_value=fake_gen):
            svc.handle_get_waveform({"file_path": "/tmp/audio.wav", "num_points": 100})

        fake_gen.generate_from_file.assert_called_once_with("/tmp/audio.wav", num_points=100)


# ---------------------------------------------------------------------------
# handle_profile_noise
# ---------------------------------------------------------------------------

class TestProfileNoise(unittest.TestCase):
    def test_missing_file_path_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_profile_noise({})

    def test_nonexistent_file_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(FileNotFoundError):
            svc.handle_profile_noise({"file_path": "/tmp/definitely_does_not_exist_12345.wav"})

    def test_delegates_to_noise_profiler(self) -> None:
        fake_result = MagicMock()
        fake_result.to_dict.return_value = {
            "noise_type": "white",
            "noise_level_db": -50.0,
            "snr_db": 30.0,
            "suitable_for_stt": True,
        }
        fake_profiler = MagicMock()
        fake_profiler.profile.return_value = fake_result

        audio_data = np.zeros(16000, dtype=np.float32)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name

        svc = _make_service()
        with patch("core.noise_profiler.NoiseProfiler", return_value=fake_profiler), \
             patch("soundfile.read", return_value=(audio_data, 16000)):
            result = svc.handle_profile_noise({"file_path": tmp_path})

        self.assertEqual(result["noise_type"], "white")
        self.assertTrue(result["suitable_for_stt"])


# ---------------------------------------------------------------------------
# handle_check_audio_duplicate
# ---------------------------------------------------------------------------

class TestCheckAudioDuplicate(unittest.TestCase):
    def test_missing_audio_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_check_audio_duplicate({"audio1": [0.0, 0.1]})

    def test_duplicate_detected(self) -> None:
        # W1125/W1063: equals() replaced compare() — exact-match fingerprint logic.
        # similarity is now binary (1.0 for match, 0.0 for non-match).
        audio_fp = MagicMock()
        audio_fp.fingerprint.return_value = [1, 2, 3]
        audio_fp.equals.return_value = True

        svc = _make_service(audio_fingerprinter=audio_fp)
        result = svc.handle_check_audio_duplicate({
            "audio1": [0.0] * 100,
            "audio2": [0.0] * 100,
            "sample_rate": 16000,
            "threshold": 0.95,
        })

        self.assertTrue(result["is_duplicate"])
        self.assertAlmostEqual(result["similarity"], 1.0, places=4)

    def test_not_duplicate_below_threshold(self) -> None:
        audio_fp = MagicMock()
        audio_fp.fingerprint.return_value = [1, 2, 3]
        audio_fp.equals.return_value = False

        svc = _make_service(audio_fingerprinter=audio_fp)
        result = svc.handle_check_audio_duplicate({
            "audio1": [0.0] * 100,
            "audio2": [0.1] * 100,
            "threshold": 0.95,
        })

        self.assertFalse(result["is_duplicate"])

    def test_custom_threshold(self) -> None:
        audio_fp = MagicMock()
        audio_fp.fingerprint.return_value = []
        audio_fp.compare.return_value = 0.80

        svc = _make_service(audio_fingerprinter=audio_fp)
        result = svc.handle_check_audio_duplicate({
            "audio1": [0.0],
            "audio2": [0.0],
            "threshold": 0.75,
        })
        self.assertTrue(result["is_duplicate"])


# ---------------------------------------------------------------------------
# Integration: BackendService.handle_request dispatches to AudioAnalyticsService
# ---------------------------------------------------------------------------

class TestAudioAnalyticsIntegration(unittest.TestCase):
    """Smoke-test: BackendService routes selected methods to AudioAnalyticsService."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.mkdtemp()

    def _make_backend(self):
        from backend.state_store import StateStore
        from backend.service import BackendService

        class _FakeRecorder:
            is_recording = False
            sample_rate = 16000
            last_stop_trim_ms = 0
            last_stop_timeout_sec = 3.0

            def start(self):
                self.is_recording = True
                return True

            def stop(self, timeout_sec=3.0, trim_tail_ms=0):
                if not self.is_recording:
                    return None
                self.is_recording = False
                return np.zeros(16000, dtype=np.float32), 1.0

        class _FakeTranscriber:
            def transcribe(self, audio, *a, **kw):
                return "ok", 0.9, []

            def load_profile(self, *a, **kw):
                pass

        store = StateStore(Path(self._tmpdir))
        svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
        )
        return svc

    def _call(self, svc, method, params=None):
        req = {"id": "t1", "method": method, "params": params or {}}
        return svc.handle_request(req)

    def test_get_audio_info_missing_path_returns_error(self) -> None:
        svc = self._make_backend()
        resp = self._call(svc, "get_audio_info", {})
        self.assertFalse(resp["ok"])

    def test_analyze_audio_quality_missing_path_returns_error(self) -> None:
        svc = self._make_backend()
        resp = self._call(svc, "analyze_audio_quality", {})
        self.assertFalse(resp["ok"])

    def test_analyze_silence_missing_path_returns_error(self) -> None:
        svc = self._make_backend()
        resp = self._call(svc, "analyze_silence", {})
        self.assertFalse(resp["ok"])

    def test_analyze_word_timing_bad_segments_returns_error(self) -> None:
        svc = self._make_backend()
        resp = self._call(svc, "analyze_word_timing", {"segments": "bad"})
        self.assertFalse(resp["ok"])

    def test_analyze_quality_trends_ok(self) -> None:
        svc = self._make_backend()
        resp = self._call(svc, "analyze_quality_trends", {"days": 7})
        self.assertTrue(resp["ok"])
        self.assertIn("overall_trend", resp["result"])

    def test_check_audio_duplicate_missing_audio_returns_error(self) -> None:
        svc = self._make_backend()
        resp = self._call(svc, "check_audio_duplicate", {"audio1": [0.0]})
        self.assertFalse(resp["ok"])


if __name__ == "__main__":
    unittest.main()
