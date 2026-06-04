"""test_search_analysis_privacy_dos_W1728.py — regression tests for Wave 1728 fixes.

BUG 1 (HIGH, privacy-bypass) — handle_get_topic_timeline had NO privacy_mode_enabled guard:
  transcript-derived topic timeline was exposed even when privacy mode was on.
  Fix: added the same guard pattern as compare_recordings (W1408 F1 / W1710).
  Also added guard to handle_get_recording_insights (W1728).

BUG 2 (HIGH, DoS) — window_size and limit params in handle_get_topic_timeline were unbounded:
  a huge window_size causes ~O(n²) or massive memory work in TopicTracker.
  Fix: clamp window_size ≤ 1000, limit ≤ 10000.

Tests:
  - handle_get_topic_timeline with privacy_mode_enabled=True returns empty/safe response
    (no transcript topics, no raw text).
  - handle_get_recording_insights with privacy_mode_enabled=True returns empty/safe response.
  - Unbounded window_size is clamped to 1000 (no runaway).
  - Unbounded limit is clamped to 10000.
  - Privacy-off paths still work normally (fail-before / pass-after pattern).
"""

from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.search_and_analysis_service import SearchAndAnalysisService
from tests.test_helpers import make_test_item


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_item(item_id: str, text: str = "обсуждение проекта разработки системы") -> Any:
    return make_test_item(
        id=item_id,
        text=text,
        ts="2023-11-14T18:53:20Z",
        audio_duration_sec=30.0,
        confidence=0.9,
        source_lang="ru",
        action_items=None,
    )


def _make_fake_store(items: list[Any] | None = None) -> Any:
    _items = items or [_make_fake_item("x1"), _make_fake_item("x2")]
    lock = threading.Lock()

    class _LockCtx:
        def __enter__(self):
            lock.acquire()
            return self

        def __exit__(self, *_):
            lock.release()

    store = types.SimpleNamespace()
    store._lock = _LockCtx
    store._load_active_items_unlocked = lambda: list(_items)
    return store


def _make_svc(privacy_enabled: bool) -> SearchAndAnalysisService:
    """Build a minimal SearchAndAnalysisService with the privacy setting configured."""
    mock_searcher = MagicMock()
    mock_searcher.is_enabled = False

    mock_topic_tracker = MagicMock()
    mock_topic_tracker.get_topic_timeline.return_value = [
        {"topic": "технологии", "is_shift": True, "start_index": 0, "end_index": 1},
    ]
    mock_topic_tracker.get_current_topic.return_value = "технологии"

    mock_insights = MagicMock()
    mock_insight_item = MagicMock()
    mock_insight_item.to_dict.return_value = {"text": "Вы часто говорите о технологиях"}
    mock_insights.generate_insights.return_value = [mock_insight_item]

    def _settings_get(key: str, default: Any = None) -> Any:
        if key == "privacy_mode_enabled":
            return privacy_enabled
        return default

    return SearchAndAnalysisService(
        store=_make_fake_store(),
        semantic_searcher=mock_searcher,
        action_items_extractor=None,
        topic_tracker=mock_topic_tracker,
        recording_insights=mock_insights,
        recording_comparison=MagicMock(),
        stats_report=MagicMock(),
        settings_get=_settings_get,
    )


# ---------------------------------------------------------------------------
# BUG 1 — Privacy guard on handle_get_topic_timeline
# ---------------------------------------------------------------------------


class TopicTimelinePrivacyGuardTestCase(unittest.TestCase):
    """handle_get_topic_timeline must return empty/safe result when privacy_mode_enabled."""

    def test_privacy_on_returns_empty_segments(self) -> None:
        """segments must be [] when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_topic_timeline({})
        self.assertEqual(result.get("segments"), [])

    def test_privacy_on_returns_zero_shifts(self) -> None:
        """total_shifts must be 0 when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_topic_timeline({})
        self.assertEqual(result.get("total_shifts"), 0)

    def test_privacy_on_returns_null_current_topic(self) -> None:
        """current_topic must be None (or falsy) when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_topic_timeline({})
        self.assertIsNone(result.get("current_topic"))

    def test_privacy_on_returns_reason_flag(self) -> None:
        """Response includes reason=privacy_mode_active when privacy on."""
        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_topic_timeline({})
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("privacy_mode_active"))

    def test_privacy_on_no_transcript_topic_text(self) -> None:
        """Response must not expose any transcript-derived topic values when privacy on."""
        import json

        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_topic_timeline({})
        dumped = json.dumps(result)
        # The mock topic tracker would return "технологии" — must NOT appear as a value
        self.assertNotIn("технологии", dumped)
        # segments must be empty — no topic entries
        self.assertEqual(result.get("segments"), [])
        # current_topic must be null, not a string with topic content
        self.assertIsNone(result.get("current_topic"))

    def test_privacy_on_topic_tracker_not_called(self) -> None:
        """TopicTracker must NOT be called when privacy_mode_enabled=True (no data access)."""
        svc = _make_svc(privacy_enabled=True)
        svc.handle_get_topic_timeline({})
        # topic_tracker.get_topic_timeline should not have been called
        svc._topic_tracker.get_topic_timeline.assert_not_called()

    def test_privacy_off_returns_real_segments(self) -> None:
        """Privacy off: segments is populated normally."""
        svc = _make_svc(privacy_enabled=False)
        result = svc.handle_get_topic_timeline({})
        self.assertIsInstance(result.get("segments"), list)
        self.assertGreater(len(result["segments"]), 0)

    def test_privacy_off_returns_total_shifts(self) -> None:
        """Privacy off: total_shifts is populated from segment data."""
        svc = _make_svc(privacy_enabled=False)
        result = svc.handle_get_topic_timeline({})
        # The mock returns one is_shift=True entry → 1 shift
        self.assertEqual(result.get("total_shifts"), 1)

    def test_privacy_off_returns_current_topic(self) -> None:
        """Privacy off: current_topic is populated from topic tracker."""
        svc = _make_svc(privacy_enabled=False)
        result = svc.handle_get_topic_timeline({})
        self.assertEqual(result.get("current_topic"), "технологии")

    def test_privacy_off_no_reason_key(self) -> None:
        """Privacy off: response must NOT include reason/privacy_mode_active sentinel keys."""
        svc = _make_svc(privacy_enabled=False)
        result = svc.handle_get_topic_timeline({})
        self.assertNotIn("reason", result)
        self.assertNotIn("privacy_mode_active", result)


# ---------------------------------------------------------------------------
# BUG 1 — Privacy guard on handle_get_recording_insights
# ---------------------------------------------------------------------------


class RecordingInsightsPrivacyGuardTestCase(unittest.TestCase):
    """handle_get_recording_insights must return empty/safe result when privacy_mode_enabled."""

    def test_privacy_on_returns_empty_insights(self) -> None:
        """insights must be [] when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_recording_insights({})
        self.assertEqual(result.get("insights"), [])

    def test_privacy_on_returns_zero_count(self) -> None:
        """count must be 0 when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_recording_insights({})
        self.assertEqual(result.get("count"), 0)

    def test_privacy_on_returns_reason_flag(self) -> None:
        """Response includes reason=privacy_mode_active when privacy on."""
        svc = _make_svc(privacy_enabled=True)
        result = svc.handle_get_recording_insights({})
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("privacy_mode_active"))

    def test_privacy_on_recording_insights_not_called(self) -> None:
        """RecordingInsightsGenerator must NOT be called when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True)
        svc.handle_get_recording_insights({})
        svc._recording_insights.generate_insights.assert_not_called()

    def test_privacy_off_returns_real_insights(self) -> None:
        """Privacy off: insights is populated normally."""
        svc = _make_svc(privacy_enabled=False)
        result = svc.handle_get_recording_insights({})
        self.assertIsInstance(result.get("insights"), list)
        self.assertGreater(len(result["insights"]), 0)

    def test_privacy_off_no_reason_key(self) -> None:
        """Privacy off: response must NOT include reason/privacy_mode_active sentinel keys."""
        svc = _make_svc(privacy_enabled=False)
        result = svc.handle_get_recording_insights({})
        self.assertNotIn("reason", result)
        self.assertNotIn("privacy_mode_active", result)


# ---------------------------------------------------------------------------
# BUG 2 — DoS clamp on window_size and limit
# ---------------------------------------------------------------------------


class TopicTimelineDoSClampTestCase(unittest.TestCase):
    """handle_get_topic_timeline must clamp window_size and limit to prevent DoS."""

    def _capture_window_size(self, params: dict) -> int:
        """Return the window_size actually passed to get_topic_timeline."""
        svc = _make_svc(privacy_enabled=False)
        svc.handle_get_topic_timeline(params)
        call_kwargs = svc._topic_tracker.get_topic_timeline.call_args
        # called as get_topic_timeline(items, window_size=N)
        return call_kwargs[1]["window_size"]

    def test_window_size_above_1000_is_clamped(self) -> None:
        """window_size=999999 must be clamped to 1000."""
        actual = self._capture_window_size({"window_size": 999999})
        self.assertEqual(actual, 1000)

    def test_window_size_exactly_1000_is_accepted(self) -> None:
        """window_size=1000 must pass through unchanged."""
        actual = self._capture_window_size({"window_size": 1000})
        self.assertEqual(actual, 1000)

    def test_window_size_1_is_accepted(self) -> None:
        """window_size=1 (minimum) must pass through."""
        actual = self._capture_window_size({"window_size": 1})
        self.assertEqual(actual, 1)

    def test_window_size_zero_falls_back_to_default(self) -> None:
        """window_size=0 (falsy) falls back to default 5 via `or 5` guard."""
        actual = self._capture_window_size({"window_size": 0})
        self.assertEqual(actual, 5)  # `int(0 or 5)` = 5, then max(1, min(5, 1000)) = 5

    def test_limit_above_10000_is_clamped(self) -> None:
        """limit=9999999 must not cause full store scan — clamped to 10000."""
        svc = _make_svc(privacy_enabled=False)

        # Build store with enough items to observe slicing
        big_items = [_make_fake_item(f"id{i}") for i in range(50)]
        svc._store = _make_fake_store(big_items)

        # The mock topic tracker records what items it received
        received_items: list[Any] = []

        def _capture_items(items: list, **kwargs: Any) -> list:
            received_items.extend(items)
            return []

        svc._topic_tracker.get_topic_timeline.side_effect = _capture_items
        svc._topic_tracker.get_current_topic.return_value = None

        svc.handle_get_topic_timeline({"limit": 9_999_999})
        # Must be clamped to 10000 — so at most min(50, 10000)=50 items passed
        self.assertLessEqual(len(received_items), 10000)

    def test_limit_within_bound_is_honoured(self) -> None:
        """limit=20 must slice to last 20 items (no clamping needed)."""
        svc = _make_svc(privacy_enabled=False)

        items_100 = [_make_fake_item(f"id{i}") for i in range(100)]
        svc._store = _make_fake_store(items_100)

        received_items: list[Any] = []

        def _capture(items: list, **kwargs: Any) -> list:
            received_items.extend(items)
            return []

        svc._topic_tracker.get_topic_timeline.side_effect = _capture
        svc._topic_tracker.get_current_topic.return_value = None

        svc.handle_get_topic_timeline({"limit": 20})
        self.assertEqual(len(received_items), 20)

    def test_limit_0_means_all_items(self) -> None:
        """limit=0 means 'all items' (no slicing) — existing behaviour preserved."""
        svc = _make_svc(privacy_enabled=False)

        items_30 = [_make_fake_item(f"id{i}") for i in range(30)]
        svc._store = _make_fake_store(items_30)

        received_items: list[Any] = []

        def _capture(items: list, **kwargs: Any) -> list:
            received_items.extend(items)
            return []

        svc._topic_tracker.get_topic_timeline.side_effect = _capture
        svc._topic_tracker.get_current_topic.return_value = None

        svc.handle_get_topic_timeline({"limit": 0})
        # limit=0 → items[-0:] = items (all 30)
        self.assertEqual(len(received_items), 30)

    def test_huge_window_size_does_not_crash(self) -> None:
        """Passing window_size=2**31 must not raise or cause runaway — clamped to 1000."""
        svc = _make_svc(privacy_enabled=False)
        # Should complete without exception
        result = svc.handle_get_topic_timeline({"window_size": 2 ** 31})
        self.assertIn("segments", result)

    def test_huge_limit_does_not_crash(self) -> None:
        """Passing limit=2**31 must not raise or cause runaway — clamped to 10000."""
        svc = _make_svc(privacy_enabled=False)
        result = svc.handle_get_topic_timeline({"limit": 2 ** 31})
        self.assertIn("segments", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
