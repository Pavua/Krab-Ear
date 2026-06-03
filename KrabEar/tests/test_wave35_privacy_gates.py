"""Tests — wave-35 privacy gates (C1 + C2 + C3).

C1 (HIGH) audio_analytics_service.handle_analyze_quality_trends:
    privacy_mode_enabled=True → {'ok': False, 'reason': 'privacy_mode_active'}
    privacy_mode_enabled=False → normal trend result

C2 (MED) search_and_analysis_service.handle_get_pending_action_items:
    privacy_mode_enabled=True → {'ok': True, 'items': [], 'pending': [], ...}
    privacy_mode_enabled=False → normal pending list

C3 (MED) search_history.handle_get_recent_searches + handle_get_popular_searches:
    privacy_mode_enabled=True → {'searches': [], 'reason': 'privacy_mode_active'}
    privacy_mode_enabled=False → normal search history
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_analytics_service import AudioAnalyticsService  # noqa: E402
from backend.search_and_analysis_service import SearchAndAnalysisService  # noqa: E402
from backend.search_history import SearchHistoryManager  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _privacy_on(key, default=None):
    if key == "privacy_mode_enabled":
        return True
    return default


def _privacy_off(key, default=None):
    return default


class _FakeStore:
    """Minimal fake StateStore (same pattern as existing test helpers)."""

    def __init__(self, items=None):
        self._items = list(items or [])
        self._load_called = False

    class _CM:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _lock(self):
        return self._CM()

    def _load_active_items_unlocked(self):
        self._load_called = True
        return self._items


def _make_fake_store(items=None):
    return _FakeStore(items=items)


# ---------------------------------------------------------------------------
# C1: AudioAnalyticsService.handle_analyze_quality_trends privacy gate
# ---------------------------------------------------------------------------

class TestQualityTrendsPrivacyGate(unittest.TestCase):
    """C1 (HIGH): analyze_quality_trends must block in privacy mode."""

    def _make_svc(self, settings_get):
        qt = MagicMock()
        report = MagicMock()
        report.daily_confidence = {"2026-06-01": 0.9}
        report.overall_trend = "stable"
        report.trend_slope = 0.0
        report.best_day = "2026-06-01"
        report.worst_day = "2026-06-01"
        report.confidence_distribution = {"high": 10}
        qt.analyze_trends.return_value = report
        return AudioAnalyticsService(
            audio_converter=MagicMock(),
            quality_trends=qt,
            audio_fingerprinter=MagicMock(),
            word_timing_analyzer=MagicMock(),
            store=_make_fake_store(),
            settings_get=settings_get,
        )

    def test_privacy_on_returns_ok_false(self):
        """privacy_mode=True → ok:False, reason:privacy_mode_active."""
        svc = self._make_svc(_privacy_on)
        result = svc.handle_analyze_quality_trends({"days": 7})
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_on_no_history_access(self):
        """privacy_mode=True → store._load_active_items_unlocked is NOT called."""
        store = _make_fake_store()
        svc = AudioAnalyticsService(
            audio_converter=MagicMock(),
            quality_trends=MagicMock(),
            audio_fingerprinter=MagicMock(),
            word_timing_analyzer=MagicMock(),
            store=store,
            settings_get=_privacy_on,
        )
        svc.handle_analyze_quality_trends({})
        self.assertFalse(store._load_called)

    def test_privacy_off_returns_trend_data(self):
        """privacy_mode=False → normal trend result with overall_trend key."""
        svc = self._make_svc(_privacy_off)
        result = svc.handle_analyze_quality_trends({"days": 7})
        self.assertIn("overall_trend", result)
        self.assertEqual(result["overall_trend"], "stable")

    def test_privacy_off_no_settings_get_works(self):
        """No settings_get provided → defaults to privacy off (normal behavior)."""
        qt = MagicMock()
        report = MagicMock()
        report.daily_confidence = {}
        report.overall_trend = "stable"
        report.trend_slope = 0.0
        report.best_day = None
        report.worst_day = None
        report.confidence_distribution = {}
        qt.analyze_trends.return_value = report
        svc = AudioAnalyticsService(
            audio_converter=MagicMock(),
            quality_trends=qt,
            audio_fingerprinter=MagicMock(),
            word_timing_analyzer=MagicMock(),
            store=_make_fake_store(),
            # no settings_get — should default to privacy=off
        )
        result = svc.handle_analyze_quality_trends({})
        self.assertIn("overall_trend", result)


# ---------------------------------------------------------------------------
# C2: SearchAndAnalysisService.handle_get_pending_action_items privacy gate
# ---------------------------------------------------------------------------

class TestPendingActionItemsPrivacyGate(unittest.TestCase):
    """C2 (MED): get_pending_action_items must block in privacy mode."""

    def _make_svc(self, settings_get, items=None):
        store = _make_fake_store(items=items or [])
        return SearchAndAnalysisService(
            store=store,
            semantic_searcher=MagicMock(),
            action_items_extractor=None,
            topic_tracker=MagicMock(),
            recording_insights=MagicMock(),
            recording_comparison=MagicMock(),
            stats_report=MagicMock(),
            settings_get=settings_get,
        )

    def test_privacy_on_returns_empty_items(self):
        """privacy_mode=True → returns ok:True with empty items list."""
        svc = self._make_svc(_privacy_on)
        result = svc.handle_get_pending_action_items({})
        # Must have empty items/pending
        self.assertEqual(result.get("items", result.get("pending", [])), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_on_no_store_access(self):
        """privacy_mode=True → store._load_active_items_unlocked is NOT accessed."""
        store = _make_fake_store()
        svc = SearchAndAnalysisService(
            store=store,
            semantic_searcher=MagicMock(),
            action_items_extractor=None,
            topic_tracker=MagicMock(),
            recording_insights=MagicMock(),
            recording_comparison=MagicMock(),
            stats_report=MagicMock(),
            settings_get=_privacy_on,
        )
        svc.handle_get_pending_action_items({})
        self.assertFalse(store._load_called)

    def test_privacy_off_returns_pending_items(self):
        """privacy_mode=False → normal pending list from store."""
        item = MagicMock()
        item.id = "item-1"
        item.ts = "2026-06-01T00:00:00Z"
        item.text = "some transcript text"
        item.action_items = None
        item.audio_duration_sec = 60.0
        svc = self._make_svc(_privacy_off, items=[item])
        result = svc.handle_get_pending_action_items({})
        pending = result.get("pending", [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], "item-1")

    def test_privacy_off_text_preview_present(self):
        """privacy_mode=False → text_preview is included in results."""
        item = MagicMock()
        item.id = "item-2"
        item.ts = "2026-06-01T00:00:00Z"
        item.text = "x" * 200
        item.action_items = None
        item.audio_duration_sec = 30.0
        svc = self._make_svc(_privacy_off, items=[item])
        result = svc.handle_get_pending_action_items({})
        pending = result.get("pending", [])
        self.assertEqual(len(pending), 1)
        # text_preview should be capped at 100 chars
        self.assertLessEqual(len(pending[0]["text_preview"]), 100)


# ---------------------------------------------------------------------------
# C3: SearchHistoryManager IPC handlers privacy gate
# ---------------------------------------------------------------------------

class TestSearchHistoryPrivacyGate(unittest.TestCase):
    """C3 (MED): get_recent_searches + get_popular_searches must block in privacy mode."""

    def _make_mgr(self, settings_fn, queries=None):
        mgr = SearchHistoryManager(settings_fn=settings_fn)
        for q in (queries or []):
            mgr.record_search(q)
        return mgr

    # --- handle_get_recent_searches ---

    def test_recent_searches_privacy_on_empty(self):
        """privacy_mode=True → empty searches list."""
        mgr = self._make_mgr(_privacy_on, queries=["secret query", "another secret"])
        result = mgr.handle_get_recent_searches({"limit": 10})
        self.assertEqual(result["searches"], [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_recent_searches_privacy_off_returns_data(self):
        """privacy_mode=False → returns normal recent searches."""
        mgr = self._make_mgr(_privacy_off, queries=["публичный запрос"])
        result = mgr.handle_get_recent_searches({"limit": 10})
        self.assertEqual(len(result["searches"]), 1)
        self.assertEqual(result["searches"][0]["query"], "публичный запрос")

    def test_recent_searches_no_settings_fn_defaults_off(self):
        """No settings_fn provided → defaults to privacy=off (backward compat)."""
        mgr = SearchHistoryManager()
        mgr.record_search("test query")
        result = mgr.handle_get_recent_searches({})
        self.assertEqual(len(result["searches"]), 1)

    # --- handle_get_popular_searches ---

    def test_popular_searches_privacy_on_empty(self):
        """privacy_mode=True → empty searches list."""
        mgr = self._make_mgr(_privacy_on, queries=["query a", "query a", "query b"])
        result = mgr.handle_get_popular_searches({"limit": 5})
        self.assertEqual(result["searches"], [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_popular_searches_privacy_off_returns_data(self):
        """privacy_mode=False → returns normal popular searches."""
        mgr = self._make_mgr(_privacy_off, queries=["freq", "freq", "freq", "rare"])
        result = mgr.handle_get_popular_searches({"limit": 5})
        self.assertGreater(len(result["searches"]), 0)
        self.assertEqual(result["searches"][0]["query"], "freq")
        self.assertEqual(result["searches"][0]["count"], 3)

    def test_popular_searches_no_settings_fn_defaults_off(self):
        """No settings_fn → privacy=off (backward compat)."""
        mgr = SearchHistoryManager()
        mgr.record_search("compat")
        result = mgr.handle_get_popular_searches({})
        self.assertEqual(len(result["searches"]), 1)

    def test_privacy_on_does_not_expose_entry_count(self):
        """Even with many entries, privacy mode returns empty list."""
        mgr = self._make_mgr(_privacy_on)
        for i in range(50):
            mgr.record_search(f"private query {i}")
        result = mgr.handle_get_recent_searches({"limit": 100})
        self.assertEqual(result["searches"], [])

    def test_privacy_switch_recent(self):
        """Switching privacy_mode from False to True changes output."""
        privacy_state = [False]

        def _dynamic(key, default=None):
            if key == "privacy_mode_enabled":
                return privacy_state[0]
            return default

        mgr = SearchHistoryManager(settings_fn=_dynamic)
        mgr.record_search("visible query")

        # Off → should return data
        result_off = mgr.handle_get_recent_searches({})
        self.assertGreater(len(result_off["searches"]), 0)

        # On → should return empty
        privacy_state[0] = True
        result_on = mgr.handle_get_recent_searches({})
        self.assertEqual(result_on["searches"], [])

    def test_privacy_switch_popular(self):
        """Switching privacy_mode from False to True changes popular output."""
        privacy_state = [False]

        def _dynamic(key, default=None):
            if key == "privacy_mode_enabled":
                return privacy_state[0]
            return default

        mgr = SearchHistoryManager(settings_fn=_dynamic)
        for _ in range(3):
            mgr.record_search("test popular")

        result_off = mgr.handle_get_popular_searches({})
        self.assertGreater(len(result_off["searches"]), 0)

        privacy_state[0] = True
        result_on = mgr.handle_get_popular_searches({})
        self.assertEqual(result_on["searches"], [])


if __name__ == "__main__":
    unittest.main()
