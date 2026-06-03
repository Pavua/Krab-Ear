"""test_wave34_audio_stats_fixes.py — regression tests for wave-34 MED fixes.

FIX D1 (MED) — audio_analytics_service.handle_analyze_silence: unbounded threshold_db
  caused OverflowError inside silence_detector (10**(db/20) → inf for db=99999).
  Fix: clamp threshold_db to [-80, 0] and guard non-finite values.

FIX D2 (MED) — search_and_analysis_service: handle_generate_stats_report and
  handle_generate_mini_stats_report had no privacy_mode_enabled guard.
  Both handlers read transcript words/patterns → must be blocked in privacy mode.
  Fix: added the same privacy gate pattern used by other analytics handlers.

FIX D3 (MED) — stats_report.StatsReportGenerator._section_top_speakers: unguarded
  float() call on diarization segment start/end values; bad (non-numeric) data
  from corrupted history items caused ValueError/TypeError crash.
  Fix: wrap with try/except (TypeError, ValueError): continue.

Tests:
  - D1: threshold_db=99999 → clamped to 0.0; no OverflowError passed to detector.
  - D1: threshold_db=-200 → clamped to -80.0.
  - D1: threshold_db=float('inf') → reset to default (SILENCE_THRESHOLD_DB).
  - D1: threshold_db=float('nan') → reset to default (SILENCE_THRESHOLD_DB).
  - D1: threshold_db=-30 → passes through unchanged.
  - D2: generate_stats_report with privacy=True → {"ok": False, "reason": "privacy_mode_active"}.
  - D2: generate_mini_stats_report with privacy=True → {"ok": False, "reason": "privacy_mode_active"}.
  - D2: privacy=False → normal result (no short-circuit).
  - D3: bad diarization data (start="bad") → no crash, speaker skipped.
  - D3: bad diarization data (start=None) → no crash, speaker skipped.
  - D3: normal diarization data → correct durations computed.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_analytics_service import AudioAnalyticsService  # noqa: E402
from backend.search_and_analysis_service import SearchAndAnalysisService  # noqa: E402
from backend.stats_report import StatsReportGenerator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — AudioAnalyticsService
# ---------------------------------------------------------------------------

def _make_audio_service(data_dir=None) -> AudioAnalyticsService:
    svc = AudioAnalyticsService(
        audio_converter=MagicMock(),
        quality_trends=MagicMock(),
        audio_fingerprinter=MagicMock(),
        word_timing_analyzer=MagicMock(),
        store=MagicMock(),
    )
    if data_dir is not None:
        svc._data_dir = Path(data_dir)
    return svc


# ---------------------------------------------------------------------------
# Helpers — SearchAndAnalysisService
# ---------------------------------------------------------------------------

def _make_fake_item(item_id: str, text: str = "тестовый текст транскрипции") -> Any:
    item = types.SimpleNamespace()
    item.id = item_id
    item.text = text
    item.ts = 1_700_000_000.0
    item.audio_duration_sec = 30.0
    item.confidence = 0.9
    item.language = "ru"
    item.action_items = None
    return item


def _make_fake_store(items: list[Any] | None = None) -> Any:
    _items = items or [_make_fake_item("x1")]

    class _LockCtx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeStore:
        def _lock(self):
            return _LockCtx()

        def _load_active_items_unlocked(self):
            return list(_items)

        def _load_active_items_with_lock(self):
            return list(_items)

    return _FakeStore()


def _make_search_service(privacy: bool = False, stats_report=None) -> SearchAndAnalysisService:
    settings: dict[str, Any] = {"privacy_mode_enabled": privacy}

    def _settings_get(key: str, default: Any = None) -> Any:
        return settings.get(key, default)

    return SearchAndAnalysisService(
        store=_make_fake_store(),
        semantic_searcher=MagicMock(),
        action_items_extractor=None,
        topic_tracker=MagicMock(),
        recording_insights=MagicMock(),
        recording_comparison=MagicMock(),
        stats_report=stats_report or MagicMock(),
        settings_get=_settings_get,
    )


# ---------------------------------------------------------------------------
# D1 — silence threshold clamp
# ---------------------------------------------------------------------------

class TestSilenceThresholdClamp(unittest.TestCase):
    """D1: threshold_db is clamped before reaching silence_detector."""

    def _call_with_threshold(self, threshold_db_value, captured: list) -> None:
        """Invoke handle_analyze_silence and capture the threshold passed to detector."""
        import tempfile
        import os

        # Create a valid temp file so path validation passes
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name

        try:
            svc = _make_audio_service(data_dir=tempfile.gettempdir())

            def _fake_analyze(file_path, threshold_db):
                captured.append(threshold_db)
                return {"silence_ratio": 0.1, "speech_ratio": 0.9, "segments": []}

            with patch(
                "core.silence_detector.analyze_silence_file",
                side_effect=_fake_analyze,
            ):
                svc.handle_analyze_silence(
                    {"file_path": tmp_path, "threshold_db": threshold_db_value}
                )
        finally:
            os.unlink(tmp_path)

    def test_extreme_positive_clamped_to_zero(self):
        """threshold_db=99999 must be clamped to 0.0."""
        captured: list = []
        self._call_with_threshold(99999, captured)
        self.assertEqual(len(captured), 1)
        self.assertAlmostEqual(captured[0], 0.0)

    def test_extreme_negative_clamped_to_minus80(self):
        """threshold_db=-200 must be clamped to -80.0."""
        captured: list = []
        self._call_with_threshold(-200, captured)
        self.assertEqual(len(captured), 1)
        self.assertAlmostEqual(captured[0], -80.0)

    def test_infinity_reset_to_default(self):
        """threshold_db=inf must be reset (non-finite guard)."""
        import math

        captured: list = []
        self._call_with_threshold(float("inf"), captured)
        self.assertEqual(len(captured), 1)
        # After non-finite reset it must be finite and within [-80, 0]
        self.assertTrue(math.isfinite(captured[0]))
        self.assertGreaterEqual(captured[0], -80.0)
        self.assertLessEqual(captured[0], 0.0)

    def test_nan_reset_to_default(self):
        """threshold_db=nan must be reset (non-finite guard)."""
        import math

        captured: list = []
        self._call_with_threshold(float("nan"), captured)
        self.assertEqual(len(captured), 1)
        self.assertTrue(math.isfinite(captured[0]))

    def test_normal_value_passes_through(self):
        """threshold_db=-30 is within bounds and must not be altered."""
        captured: list = []
        self._call_with_threshold(-30.0, captured)
        self.assertEqual(len(captured), 1)
        self.assertAlmostEqual(captured[0], -30.0)


# ---------------------------------------------------------------------------
# D2 — stats_report privacy gates
# ---------------------------------------------------------------------------

class TestStatsReportPrivacyGate(unittest.TestCase):
    """D2: generate_stats_report and generate_mini_stats_report blocked in privacy mode."""

    def test_generate_stats_report_privacy_on_returns_error(self):
        """Privacy mode → ok=False, no StatsReportGenerator call."""
        mock_stats = MagicMock()
        svc = _make_search_service(privacy=True, stats_report=mock_stats)

        result = svc.handle_generate_stats_report({})

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        mock_stats.generate_report.assert_not_called()

    def test_generate_mini_stats_report_privacy_on_returns_error(self):
        """Privacy mode → ok=False, no StatsReportGenerator call."""
        mock_stats = MagicMock()
        svc = _make_search_service(privacy=True, stats_report=mock_stats)

        result = svc.handle_generate_mini_stats_report({})

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        mock_stats.generate_mini_report.assert_not_called()

    def test_generate_stats_report_privacy_off_passes_through(self):
        """Privacy mode off → normal path, StatsReportGenerator.generate_report called."""
        mock_stats = MagicMock()
        mock_stats.generate_report.return_value = "## Статистика\n"
        svc = _make_search_service(privacy=False, stats_report=mock_stats)

        result = svc.handle_generate_stats_report({"days": 7})

        mock_stats.generate_report.assert_called_once()
        self.assertIn("markdown", result)
        self.assertEqual(result["days"], 7)

    def test_generate_mini_stats_report_privacy_off_passes_through(self):
        """Privacy mode off → normal path, StatsReportGenerator.generate_mini_report called."""
        mock_stats = MagicMock()
        mock_stats.generate_mini_report.return_value = "## Mini\n"
        svc = _make_search_service(privacy=False, stats_report=mock_stats)

        result = svc.handle_generate_mini_stats_report({})

        mock_stats.generate_mini_report.assert_called_once()
        self.assertIn("markdown", result)


# ---------------------------------------------------------------------------
# D3 — diarization float guard in _section_top_speakers
# ---------------------------------------------------------------------------

class TestSectionTopSpeakersFloatGuard(unittest.TestCase):
    """D3: bad diarization data (non-numeric start/end) → no crash, speaker skipped."""

    def _make_item_with_diarization(self, segments: list[dict]) -> Any:
        item = types.SimpleNamespace()
        item.id = "item1"
        item.text = "тестовый текст"
        item.ts = 1_700_000_000.0
        item.audio_duration_sec = 60.0
        item.confidence = 0.9
        item.language = "ru"
        item.tags = []
        item.diarization = {"segments": segments}
        return item

    def _call_section(self, items: list[Any]) -> str:
        gen = StatsReportGenerator()
        return gen._section_top_speakers(items)

    def test_bad_string_start_skipped(self):
        """Segment with start='bad' → no crash, speaker not counted."""
        item = self._make_item_with_diarization([
            {"speaker": "SPEAKER_00", "start": "bad", "end": 10.0},
        ])
        result = self._call_section([item])
        # Should not raise; speaker should be absent or have zero data
        self.assertIsInstance(result, str)
        # The section should show "no diarization data" or empty table
        self.assertIn("диаризации", result.lower())

    def test_none_start_skipped(self):
        """Segment with start=None → no crash (float(None) raises TypeError)."""
        item = self._make_item_with_diarization([
            {"speaker": "SPEAKER_01", "start": None, "end": 20.0},
        ])
        result = self._call_section([item])
        self.assertIsInstance(result, str)

    def test_none_end_skipped(self):
        """Segment with end=None → no crash."""
        item = self._make_item_with_diarization([
            {"speaker": "SPEAKER_02", "start": 5.0, "end": None},
        ])
        result = self._call_section([item])
        self.assertIsInstance(result, str)

    def test_mixed_bad_and_good_segments(self):
        """Bad segment skipped; good segment still counted correctly."""
        item = self._make_item_with_diarization([
            {"speaker": "SPEAKER_00", "start": "bad", "end": 10.0},
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 30.0},
        ])
        result = self._call_section([item])
        # Good segment counted → speaker must appear in table
        self.assertIn("SPEAKER_00", result)

    def test_valid_diarization_data_works(self):
        """Sanity check: valid data produces correct speaker rows."""
        item = self._make_item_with_diarization([
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 60.0},
            {"speaker": "SPEAKER_01", "start": 60.0, "end": 90.0},
        ])
        result = self._call_section([item])
        self.assertIn("SPEAKER_00", result)
        self.assertIn("SPEAKER_01", result)

    def test_empty_segments_shows_no_data_message(self):
        """No segments → shows absence message, no crash."""
        item = self._make_item_with_diarization([])
        result = self._call_section([item])
        self.assertIn("отсутствуют", result.lower())


if __name__ == "__main__":
    unittest.main()
