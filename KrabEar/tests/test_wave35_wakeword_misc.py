"""Tests for wave-35 fixes: wakeword privacy gate (MED) + find_duplicates threshold clamp (LOW)
+ analytics days int() guard (LOW).

E1 (MED) — handle_wake_word_start: privacy_mode_enabled=True must return ok=False.
E2 (LOW) — handle_find_duplicates: similarity_threshold clamped to [0.0, 1.0].
E3 (LOW) — get_sentiment_trends: non-numeric days param must default to 30, not crash.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402
from backend.analytics_service import AnalyticsService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wakeword_adapter(
    tmp_dir: str | Path,
    settings: dict | None = None,
) -> OpenWakeWordAdapter:
    settings = settings or {}
    adapter = OpenWakeWordAdapter(
        data_dir=tmp_dir,
        settings_get=lambda k, d: settings.get(k, d),
    )
    adapter._oww_available = False  # no real library needed
    return adapter


class _FakeStore:
    class _CM:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _lock(self):
        return self._CM()

    def _load_active_items_unlocked(self):
        return []

    def _load_active_items_with_lock(self):
        return []

    def get_history_page_filtered(self, cursor, limit, paste_status, translation_mode,
                                  translation_status=None, from_ts=None, to_ts=None):
        return [], None


def _make_analytics_service() -> AnalyticsService:
    mock_trends = MagicMock()
    mock_trends.analyze_sentiment_trends.return_value = MagicMock()
    mock_trends.to_dict.return_value = {
        "ok": True,
        "daily_sentiment": [],
        "overall_sentiment": 0.0,
        "mood_trend": "stable",
        "sentiment_distribution": {"positive": 0, "negative": 0, "neutral": 0},
        "most_positive_day": {},
        "most_negative_day": {},
    }
    return AnalyticsService(
        analytics_dashboard=MagicMock(),
        sentiment_trends=mock_trends,
        activity_calendar=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        timeline_view=MagicMock(),
        store=_FakeStore(),
    )


# ---------------------------------------------------------------------------
# E1 — wakeword privacy gate must return ok=False
# ---------------------------------------------------------------------------

class TestWakeWordPrivacyGate(unittest.TestCase):
    """E1 (MED): privacy_mode_enabled=True must block wake-word activation."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_privacy_mode_returns_ok_false(self) -> None:
        """Core assertion: privacy mode must return ok=False with reason field."""
        adapter = _make_wakeword_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": True},
        )
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertIn("reason", result)
        self.assertEqual(result["reason"], "cannot activate wake-word in privacy mode")

    def test_privacy_mode_reason_present(self) -> None:
        """Result must carry informative reason string, not just ok=False."""
        adapter = _make_wakeword_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": True},
        )
        result = adapter.handle_wake_word_start({})
        self.assertFalse(result["ok"])
        self.assertIn("privacy mode", result.get("reason", "").lower())

    def test_privacy_mode_blocks_before_threshold_check(self) -> None:
        """Privacy gate must fire even when threshold would also be invalid."""
        adapter = _make_wakeword_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": True},
        )
        # threshold=-0.5 would normally trigger a separate rejection;
        # privacy gate fires first and returns ok=False with reason
        result = adapter.handle_wake_word_start({"model": "hey_jarvis", "threshold": -0.5})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "cannot activate wake-word in privacy mode")

    def test_privacy_mode_false_allows_start(self) -> None:
        """When privacy_mode_enabled=False the call must reach start()."""
        adapter = _make_wakeword_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": False},
        )
        with patch.object(adapter, "start"):
            result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotIn("cannot activate wake-word in privacy mode", result.get("reason", ""))

    def test_privacy_mode_absent_allows_start(self) -> None:
        """When privacy_mode_enabled key is absent the call must not be blocked."""
        adapter = _make_wakeword_adapter(self._tmp, settings={})
        with patch.object(adapter, "start"):
            result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotIn("cannot activate wake-word in privacy mode", result.get("reason", ""))


# ---------------------------------------------------------------------------
# E2 — handle_find_duplicates threshold clamping
# ---------------------------------------------------------------------------

class TestFindDuplicatesThresholdClamp(unittest.TestCase):
    """E2 (LOW): similarity_threshold must be clamped to [0.0, 1.0]."""

    def setUp(self) -> None:
        self._store = _FakeStore()

    def _call_find_duplicates(self, threshold_param):
        """Helper: import HistoryService and call handle_find_duplicates."""
        from backend.history_service import HistoryService
        svc = HistoryService.__new__(HistoryService)
        svc.store = self._store
        # Stub DuplicateDetector so we can inspect what threshold was passed
        with patch("backend.history_service.DuplicateDetector") as MockDD:
            instance = MockDD.return_value
            instance.find_duplicates.return_value = []
            svc.handle_find_duplicates({"similarity_threshold": threshold_param})
            return instance.find_duplicates.call_args

    def test_negative_threshold_clamped_to_zero(self) -> None:
        """similarity_threshold=-0.5 must be clamped to 0.0."""
        call_args = self._call_find_duplicates(-0.5)
        _, kwargs = call_args
        self.assertAlmostEqual(kwargs["similarity_threshold"], 0.0)

    def test_threshold_above_one_clamped_to_one(self) -> None:
        """similarity_threshold=1.5 must be clamped to 1.0."""
        call_args = self._call_find_duplicates(1.5)
        _, kwargs = call_args
        self.assertAlmostEqual(kwargs["similarity_threshold"], 1.0)

    def test_threshold_in_range_unchanged(self) -> None:
        """similarity_threshold=0.7 must pass through unchanged."""
        call_args = self._call_find_duplicates(0.7)
        _, kwargs = call_args
        self.assertAlmostEqual(kwargs["similarity_threshold"], 0.7)

    def test_threshold_zero_clamped_to_zero(self) -> None:
        """similarity_threshold=0.0 is valid (boundary) and must stay 0.0."""
        call_args = self._call_find_duplicates(0.0)
        _, kwargs = call_args
        self.assertAlmostEqual(kwargs["similarity_threshold"], 0.0)

    def test_threshold_one_unchanged(self) -> None:
        """similarity_threshold=1.0 is valid (boundary) and must stay 1.0."""
        call_args = self._call_find_duplicates(1.0)
        _, kwargs = call_args
        self.assertAlmostEqual(kwargs["similarity_threshold"], 1.0)


# ---------------------------------------------------------------------------
# E3 — get_sentiment_trends days guard
# ---------------------------------------------------------------------------

class TestSentimentTrendsDaysGuard(unittest.TestCase):
    """E3 (LOW): non-numeric days param must not crash; must default to 30."""

    def test_string_days_does_not_crash(self) -> None:
        """days='abc' must not raise; must produce a valid response."""
        svc = _make_analytics_service()
        try:
            result = svc.handle_get_sentiment_trends({"days": "abc"})
        except (TypeError, ValueError) as exc:
            self.fail(f"handle_get_sentiment_trends raised on non-numeric days: {exc}")
        # Must return some dict (not None, not exception)
        self.assertIsInstance(result, dict)

    def test_none_days_does_not_crash(self) -> None:
        """days=None must not raise."""
        svc = _make_analytics_service()
        try:
            result = svc.handle_get_sentiment_trends({"days": None})
        except (TypeError, ValueError) as exc:
            self.fail(f"handle_get_sentiment_trends raised on None days: {exc}")
        self.assertIsInstance(result, dict)

    def test_string_days_defaults_to_30(self) -> None:
        """days='abc' must call analyze_sentiment_trends with days=30."""
        mock_trends = MagicMock()
        mock_trends.analyze_sentiment_trends.return_value = MagicMock()
        mock_trends.to_dict.return_value = {"ok": True}
        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=mock_trends,
            activity_calendar=MagicMock(),
            keyword_cloud_gen=MagicMock(),
            timeline_view=MagicMock(),
            store=_FakeStore(),
        )
        svc.handle_get_sentiment_trends({"days": "abc"})
        call_kwargs = mock_trends.analyze_sentiment_trends.call_args[1]
        self.assertEqual(call_kwargs["days"], 30)

    def test_numeric_string_days_parsed(self) -> None:
        """days='14' (numeric string) must be parsed to int 14."""
        mock_trends = MagicMock()
        mock_trends.analyze_sentiment_trends.return_value = MagicMock()
        mock_trends.to_dict.return_value = {"ok": True}
        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=mock_trends,
            activity_calendar=MagicMock(),
            keyword_cloud_gen=MagicMock(),
            timeline_view=MagicMock(),
            store=_FakeStore(),
        )
        svc.handle_get_sentiment_trends({"days": "14"})
        call_kwargs = mock_trends.analyze_sentiment_trends.call_args[1]
        self.assertEqual(call_kwargs["days"], 14)

    def test_list_days_does_not_crash(self) -> None:
        """days=[1, 2] (wrong type) must not raise, must default to 30."""
        mock_trends = MagicMock()
        mock_trends.analyze_sentiment_trends.return_value = MagicMock()
        mock_trends.to_dict.return_value = {"ok": True}
        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=mock_trends,
            activity_calendar=MagicMock(),
            keyword_cloud_gen=MagicMock(),
            timeline_view=MagicMock(),
            store=_FakeStore(),
        )
        try:
            svc.handle_get_sentiment_trends({"days": [1, 2]})
        except (TypeError, ValueError) as exc:
            self.fail(f"Raised on list days: {exc}")
        call_kwargs = mock_trends.analyze_sentiment_trends.call_args[1]
        self.assertEqual(call_kwargs["days"], 30)


if __name__ == "__main__":
    unittest.main()
