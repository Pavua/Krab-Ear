<<<<<<< HEAD
"""Unit tests — AnalyticsService (6 IPC handlers).

Tests each handler directly against mocked collaborators, then an integration
smoke-test exercises them via BackendService.handle_request dispatch.
Extracted from BackendService Wave 392.
=======
"""Unit tests — AnalyticsService (8 IPC handlers).

Tests each handler directly against mocked collaborators.
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
"""

from __future__ import annotations

import sys
<<<<<<< HEAD
import tempfile
=======
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.analytics_service import AnalyticsService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal fake StateStore for AnalyticsService tests."""

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
        return list(self._items)

<<<<<<< HEAD
    def _load_active_items_with_lock(self):
        return list(self._items)


def _make_service(
    analytics_dashboard=None,
    sentiment_trends=None,
    activity_calendar=None,
    keyword_cloud_gen=None,
    timeline_view=None,
    store=None,
) -> AnalyticsService:
    return AnalyticsService(
        analytics_dashboard=analytics_dashboard or MagicMock(),
        sentiment_trends=sentiment_trends or MagicMock(),
        activity_calendar=activity_calendar or MagicMock(),
        keyword_cloud_gen=keyword_cloud_gen or MagicMock(),
        timeline_view=timeline_view or MagicMock(),
        store=store or _FakeStore(),
    )


# ---------------------------------------------------------------------------
# handle_get_analytics_dashboard
# ---------------------------------------------------------------------------

class TestGetAnalyticsDashboard(unittest.TestCase):
    def test_delegates_to_dashboard(self) -> None:
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {"overview": {"total": 5}}
        svc = _make_service(analytics_dashboard=dashboard)

        result = svc.handle_get_analytics_dashboard({})

        dashboard.get_full_dashboard.assert_called_once()
        self.assertEqual(result["overview"]["total"], 5)

    def test_days_clamped_to_range(self) -> None:
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {}
        svc = _make_service(analytics_dashboard=dashboard)

        # days=0 is falsy — `int(params.get("days", 30) or 30)` => 30 (default behaviour)
        # Clamping to 1 only applies to explicit negative values; days=0 uses default=30.
        svc.handle_get_analytics_dashboard({"days": 0})
        call_kwargs = dashboard.get_full_dashboard.call_args[1]
        self.assertEqual(call_kwargs["days"], 30)

    def test_days_clamped_max(self) -> None:
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {}
        svc = _make_service(analytics_dashboard=dashboard)

        # days=9999 should be clamped to 365
=======

def _session_state():
    return {
        "session": {
            "recording_active": False,
            "preview_active": False,
            "preview_text_length": 0,
            "preview_duration_sec": 0.0,
        },
        "preview_loop": {"error_count": 0, "last_reset_ts": 0.0},
        "llm_status": None,
        "call_assist": {"active": False},
        "import": {"active": False},
    }


def _settings():
    return {
        "llm_rewrite_enabled": False,
        "llm_model": "test-model",
        "quality_profile": "balanced",
        "cleanup_profile": "soft",
        "translation_mode": "off",
        "diarization_enabled": False,
        "network_mode": "offline_default",
    }


def _make_svc(**overrides) -> AnalyticsService:
    defaults = dict(
        analytics_dashboard=MagicMock(),
        daily_digest=MagicMock(),
        activity_calendar=MagicMock(),
        recording_insights=MagicMock(),
        sentiment_trends=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        store=_FakeStore(),
        get_session_state=_session_state,
        get_settings=_settings,
    )
    defaults.update(overrides)
    return AnalyticsService(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandleGetAnalyticsDashboard(unittest.TestCase):

    def test_delegates_to_dashboard(self):
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {"overview": {}}
        svc = _make_svc(analytics_dashboard=dashboard, store=_FakeStore())
        result = svc.handle_get_analytics_dashboard({"days": 14})
        dashboard.get_full_dashboard.assert_called_once()
        call_kwargs = dashboard.get_full_dashboard.call_args[1]
        self.assertEqual(call_kwargs["days"], 14)
        self.assertIn("overview", result)

    def test_clamps_days_to_max_365(self):
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {}
        svc = _make_svc(analytics_dashboard=dashboard)
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
        svc.handle_get_analytics_dashboard({"days": 9999})
        call_kwargs = dashboard.get_full_dashboard.call_args[1]
        self.assertEqual(call_kwargs["days"], 365)

<<<<<<< HEAD
    def test_default_days_is_30(self) -> None:
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {}
        svc = _make_service(analytics_dashboard=dashboard)

=======
    def test_clamps_days_to_min_1(self):
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {}
        svc = _make_svc(analytics_dashboard=dashboard)
        svc.handle_get_analytics_dashboard({"days": -5})
        call_kwargs = dashboard.get_full_dashboard.call_args[1]
        self.assertEqual(call_kwargs["days"], 1)

    def test_default_days_is_30(self):
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {}
        svc = _make_svc(analytics_dashboard=dashboard)
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
        svc.handle_get_analytics_dashboard({})
        call_kwargs = dashboard.get_full_dashboard.call_args[1]
        self.assertEqual(call_kwargs["days"], 30)


<<<<<<< HEAD
# ---------------------------------------------------------------------------
# handle_get_sentiment_trends
# ---------------------------------------------------------------------------

class TestGetSentimentTrends(unittest.TestCase):
    def test_calls_analyze_and_to_dict(self) -> None:
        fake_report = MagicMock()
        sentiment = MagicMock()
        sentiment.analyze_sentiment_trends.return_value = fake_report
        sentiment.to_dict.return_value = {"trend": "stable"}

        svc = _make_service(sentiment_trends=sentiment, store=_FakeStore())
        result = svc.handle_get_sentiment_trends({"days": 7})

        sentiment.analyze_sentiment_trends.assert_called_once()
        sentiment.to_dict.assert_called_once_with(fake_report)
        self.assertEqual(result["trend"], "stable")

    def test_default_days_30(self) -> None:
        sentiment = MagicMock()
        sentiment.analyze_sentiment_trends.return_value = MagicMock()
        sentiment.to_dict.return_value = {}
        svc = _make_service(sentiment_trends=sentiment, store=_FakeStore())

        svc.handle_get_sentiment_trends({})
        call_args = sentiment.analyze_sentiment_trends.call_args
        self.assertEqual(call_args[1]["days"], 30)

    def test_store_exception_falls_back_to_empty(self) -> None:
        """When store raises during item load, handler gracefully uses empty list."""

        class _ErrorStore:
            def _lock(self):
                raise RuntimeError("disk error")

            def _load_active_items_unlocked(self):
                return []

        sentiment = MagicMock()
        sentiment.analyze_sentiment_trends.return_value = MagicMock()
        sentiment.to_dict.return_value = {"trend": "stable"}

        svc = _make_service(sentiment_trends=sentiment, store=_ErrorStore())
        result = svc.handle_get_sentiment_trends({"days": 14})
        self.assertEqual(result["trend"], "stable")


# ---------------------------------------------------------------------------
# handle_compare_periods
# ---------------------------------------------------------------------------

class TestComparePeriods(unittest.TestCase):
    def test_missing_params_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_compare_periods({"period1_start": "2024-01-01"})

    def test_all_params_required(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_compare_periods({
                "period1_start": "2024-01-01",
                "period1_end": "2024-01-31",
                # missing period2_*
            })

    def test_delegates_to_compare_periods_fn(self) -> None:
        fake_period = MagicMock()
        fake_period.recordings = 10
        fake_period.duration_sec = 3600.0
        fake_period.words = 500
        fake_period.avg_confidence = 0.92
=======
class TestHandleGenerateDailyDigest(unittest.TestCase):

    def _make_digest(self):
        d = MagicMock()
        d.date = "2026-05-19"
        d.total_recordings = 5
        d.total_duration_min = 12.5
        d.total_words = 800
        d.languages_used = ["ru"]
        d.top_topics = ["test"]
        d.highlights = []
        d.formatted_markdown = "# Digest"
        return d

    def test_returns_all_fields(self):
        daily_digest = MagicMock()
        daily_digest.generate_digest.return_value = self._make_digest()
        svc = _make_svc(daily_digest=daily_digest)
        result = svc.handle_generate_daily_digest({"date": "2026-05-19"})
        self.assertEqual(result["date"], "2026-05-19")
        self.assertEqual(result["total_recordings"], 5)
        self.assertIn("markdown", result)

    def test_date_none_passes_through(self):
        daily_digest = MagicMock()
        daily_digest.generate_digest.return_value = self._make_digest()
        svc = _make_svc(daily_digest=daily_digest)
        svc.handle_generate_daily_digest({})
        call_kwargs = daily_digest.generate_digest.call_args[1]
        self.assertIsNone(call_kwargs["date_str"])


class TestHandleComparePeriods(unittest.TestCase):

    def test_raises_on_missing_params(self):
        svc = _make_svc()
        with self.assertRaises(ValueError):
            svc.handle_compare_periods({})

    def test_raises_on_partial_params(self):
        svc = _make_svc()
        with self.assertRaises(ValueError):
            svc.handle_compare_periods({
                "period1_start": "2026-01-01",
                "period1_end": "2026-01-31",
            })

    def test_returns_comparison_structure(self):
        fake_period = MagicMock()
        fake_period.recordings = 3
        fake_period.duration_sec = 120.0
        fake_period.words = 200
        fake_period.avg_confidence = 0.9
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
        fake_period.languages = ["ru"]

        fake_report = MagicMock()
        fake_report.period1 = fake_period
        fake_report.period2 = fake_period
<<<<<<< HEAD
        fake_report.recordings_change_pct = 5.0
        fake_report.duration_change_pct = -2.0
        fake_report.confidence_change = 0.01
        fake_report.new_languages = []
        fake_report.summary = "Stable"

        svc = _make_service()

        with patch("backend.period_comparison.compare_periods", return_value=fake_report) as mock_fn:
            result = svc.handle_compare_periods({
                "period1_start": "2024-01-01",
                "period1_end": "2024-01-31",
                "period2_start": "2024-02-01",
                "period2_end": "2024-02-29",
            })

        mock_fn.assert_called_once()
        self.assertEqual(result["period1"]["recordings"], 10)
        self.assertEqual(result["recordings_change_pct"], 5.0)
        self.assertEqual(result["summary"], "Stable")

    def test_result_structure(self) -> None:
        fake_period = MagicMock(recordings=3, duration_sec=100.0, words=50, avg_confidence=0.9, languages=["es"])
        fake_report = MagicMock(
            period1=fake_period, period2=fake_period,
            recordings_change_pct=0.0, duration_change_pct=0.0,
            confidence_change=0.0, new_languages=[], summary="Equal"
        )
        svc = _make_service()
        with patch("backend.period_comparison.compare_periods", return_value=fake_report):
            result = svc.handle_compare_periods({
                "period1_start": "2024-01-01", "period1_end": "2024-01-31",
                "period2_start": "2024-02-01", "period2_end": "2024-02-29",
            })
        self.assertIn("period1", result)
        self.assertIn("period2", result)
        self.assertIn("recordings_change_pct", result)
        self.assertIn("summary", result)


# ---------------------------------------------------------------------------
# handle_get_keyword_cloud
# ---------------------------------------------------------------------------

class TestGetKeywordCloud(unittest.TestCase):
    def _make_cloud_word(self, word, count=10, weight=0.5, font_size=14):
        cw = MagicMock()
        cw.word = word
        cw.count = count
        cw.weight = weight
        cw.font_size = font_size
        return cw

    def test_returns_word_list(self) -> None:
        cloud_gen = MagicMock()
        cloud_gen.generate_cloud.return_value = [
            self._make_cloud_word("python"),
            self._make_cloud_word("краб"),
        ]
        svc = _make_service(keyword_cloud_gen=cloud_gen, store=_FakeStore())

        result = svc.handle_get_keyword_cloud({})

        self.assertIn("words", result)
        self.assertEqual(len(result["words"]), 2)
        self.assertEqual(result["words"][0]["word"], "python")

    def test_default_max_words_100(self) -> None:
        cloud_gen = MagicMock()
        cloud_gen.generate_cloud.return_value = []
        svc = _make_service(keyword_cloud_gen=cloud_gen, store=_FakeStore())

        svc.handle_get_keyword_cloud({})

        call_kwargs = cloud_gen.generate_cloud.call_args[1]
        self.assertEqual(call_kwargs["max_words"], 100)

    def test_language_passed_through(self) -> None:
        cloud_gen = MagicMock()
        cloud_gen.generate_cloud.return_value = []
        svc = _make_service(keyword_cloud_gen=cloud_gen, store=_FakeStore())

        svc.handle_get_keyword_cloud({"language": "ru", "max_words": 50})

        call_kwargs = cloud_gen.generate_cloud.call_args[1]
        self.assertEqual(call_kwargs["language"], "ru")
        self.assertEqual(call_kwargs["max_words"], 50)

    def test_word_fields_present(self) -> None:
        cloud_gen = MagicMock()
        cloud_gen.generate_cloud.return_value = [self._make_cloud_word("test")]
        svc = _make_service(keyword_cloud_gen=cloud_gen, store=_FakeStore())

        result = svc.handle_get_keyword_cloud({})

        word_entry = result["words"][0]
        for field in ("word", "count", "weight", "font_size"):
            self.assertIn(field, word_entry)


# ---------------------------------------------------------------------------
# handle_get_timeline_view
# ---------------------------------------------------------------------------

class TestGetTimelineView(unittest.TestCase):
    def _make_block(self, group_key="2024-01"):
        block = MagicMock()
        block.to_dict.return_value = {"group_key": group_key, "count": 5}
        return block

    def test_returns_blocks(self) -> None:
        timeline = MagicMock()
        timeline.generate_timeline.return_value = [self._make_block("2024-01")]
        svc = _make_service(timeline_view=timeline, store=_FakeStore())

        result = svc.handle_get_timeline_view({})

        self.assertIn("blocks", result)
        self.assertEqual(len(result["blocks"]), 1)
        self.assertEqual(result["total_blocks"], 1)

    def test_default_group_by_day(self) -> None:
        timeline = MagicMock()
        timeline.generate_timeline.return_value = []
        svc = _make_service(timeline_view=timeline, store=_FakeStore())

        result = svc.handle_get_timeline_view({})

        self.assertEqual(result["group_by"], "day")

    def test_custom_group_by(self) -> None:
        timeline = MagicMock()
        timeline.generate_timeline.return_value = []
        svc = _make_service(timeline_view=timeline, store=_FakeStore())

        result = svc.handle_get_timeline_view({"group_by": "week"})

        self.assertEqual(result["group_by"], "week")

    def test_include_heatmap(self) -> None:
        timeline = MagicMock()
        timeline.generate_timeline.return_value = []
        timeline.generate_activity_heatmap.return_value = {"days": []}
        svc = _make_service(timeline_view=timeline, store=_FakeStore())

        result = svc.handle_get_timeline_view({"include_heatmap": True})

        timeline.generate_activity_heatmap.assert_called_once()
        self.assertIn("activity_heatmap", result)

    def test_limit_clamped(self) -> None:
        """limit=0 or >5000 should be clamped."""
        timeline = MagicMock()
        timeline.generate_timeline.return_value = []
        svc = _make_service(timeline_view=timeline, store=_FakeStore())

        svc.handle_get_timeline_view({"limit": 99999})
        # No exception raised — limit clamped to 5000 internally


# ---------------------------------------------------------------------------
# handle_get_activity_calendar
# ---------------------------------------------------------------------------

class TestGetActivityCalendar(unittest.TestCase):
    def _make_calendar_result(self):
        cal = MagicMock()
        cal.to_dict.return_value = {"weeks": [], "total_recordings": 0}
        return cal

    def test_returns_calendar_dict(self) -> None:
        activity_cal = MagicMock()
        activity_cal.generate_calendar.return_value = self._make_calendar_result()
        svc = _make_service(activity_calendar=activity_cal, store=_FakeStore())

        result = svc.handle_get_activity_calendar({})

        self.assertIn("weeks", result)
        self.assertIn("total_recordings", result)

    def test_default_months_12(self) -> None:
        activity_cal = MagicMock()
        activity_cal.generate_calendar.return_value = self._make_calendar_result()
        svc = _make_service(activity_calendar=activity_cal, store=_FakeStore())

        svc.handle_get_activity_calendar({})
        call_kwargs = activity_cal.generate_calendar.call_args[1]
        self.assertEqual(call_kwargs["months"], 12)

    def test_months_clamped(self) -> None:
        activity_cal = MagicMock()
        activity_cal.generate_calendar.return_value = self._make_calendar_result()
        svc = _make_service(activity_calendar=activity_cal, store=_FakeStore())

        svc.handle_get_activity_calendar({"months": 99})
        call_kwargs = activity_cal.generate_calendar.call_args[1]
        self.assertEqual(call_kwargs["months"], 24)

    def test_include_svg(self) -> None:
        activity_cal = MagicMock()
        activity_cal.generate_calendar.return_value = self._make_calendar_result()
        activity_cal.generate_calendar_svg.return_value = "<svg/>"
        svc = _make_service(activity_calendar=activity_cal, store=_FakeStore())

        result = svc.handle_get_activity_calendar({"include_svg": True})

        activity_cal.generate_calendar_svg.assert_called_once()
        self.assertIn("svg", result)
        self.assertEqual(result["svg"], "<svg/>")

    def test_no_svg_by_default(self) -> None:
        activity_cal = MagicMock()
        activity_cal.generate_calendar.return_value = self._make_calendar_result()
        svc = _make_service(activity_calendar=activity_cal, store=_FakeStore())

        result = svc.handle_get_activity_calendar({})

        activity_cal.generate_calendar_svg.assert_not_called()
        self.assertNotIn("svg", result)

    def test_store_exception_fallback(self) -> None:
        """When store raises during item load, handler gracefully uses empty list."""

        class _ErrorStore:
            def _lock(self):
                raise RuntimeError("io error")

            def _load_active_items_unlocked(self):
                return []

        activity_cal = MagicMock()
        activity_cal.generate_calendar.return_value = self._make_calendar_result()
        svc = _make_service(activity_calendar=activity_cal, store=_ErrorStore())

        # Should not raise
        result = svc.handle_get_activity_calendar({})
        self.assertIn("weeks", result)


# ---------------------------------------------------------------------------
# Integration: BackendService.handle_request dispatch
# ---------------------------------------------------------------------------

class TestAnalyticsServiceIntegration(unittest.TestCase):
    """Smoke-tests that verify each analytics method reaches AnalyticsService
    via BackendService.handle_request dispatch table."""

    def _make_backend(self, tmp_path):
        """Build a minimal BackendService with heavy deps patched out."""
        from unittest.mock import patch as _patch, MagicMock as _MM
        import importlib

        with _patch("backend.service.AudioRecorder"), \
             _patch("backend.service.Transcriber"), \
             _patch("backend.service.Translator"), \
             _patch("backend.service.LLMRewriter", return_value=None), \
             _patch("backend.service.ActionItemsExtractor", return_value=_MM()), \
             _patch("backend.service.settings"):
            from backend.state_store import StateStore
            from backend.service import BackendService
            store = StateStore(data_dir=tmp_path)
            svc = BackendService(store=store)
        return svc

    def _dispatch(self, svc, method, params=None):
        return svc.handle_request({"id": "t1", "method": method, "params": params or {}})

    def test_get_analytics_dashboard_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                svc = self._make_backend(Path(tmp))
            except Exception:
                self.skipTest("BackendService init requires heavy deps")
            svc._analytics_svc = MagicMock()
            svc._analytics_svc.handle_get_analytics_dashboard.return_value = {"overview": {}}
            result = self._dispatch(svc, "get_analytics_dashboard", {"days": 7})
            svc._analytics_svc.handle_get_analytics_dashboard.assert_called_once()

    def test_get_sentiment_trends_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                svc = self._make_backend(Path(tmp))
            except Exception:
                self.skipTest("BackendService init requires heavy deps")
            svc._analytics_svc = MagicMock()
            svc._analytics_svc.handle_get_sentiment_trends.return_value = {"trend": "stable"}
            self._dispatch(svc, "get_sentiment_trends", {"days": 7})
            svc._analytics_svc.handle_get_sentiment_trends.assert_called_once()

    def test_get_keyword_cloud_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                svc = self._make_backend(Path(tmp))
            except Exception:
                self.skipTest("BackendService init requires heavy deps")
            svc._analytics_svc = MagicMock()
            svc._analytics_svc.handle_get_keyword_cloud.return_value = {"words": []}
            self._dispatch(svc, "get_keyword_cloud", {})
            svc._analytics_svc.handle_get_keyword_cloud.assert_called_once()

    def test_get_timeline_view_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                svc = self._make_backend(Path(tmp))
            except Exception:
                self.skipTest("BackendService init requires heavy deps")
            svc._analytics_svc = MagicMock()
            svc._analytics_svc.handle_get_timeline_view.return_value = {"blocks": []}
            self._dispatch(svc, "get_timeline_view", {})
            svc._analytics_svc.handle_get_timeline_view.assert_called_once()

    def test_get_activity_calendar_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                svc = self._make_backend(Path(tmp))
            except Exception:
                self.skipTest("BackendService init requires heavy deps")
            svc._analytics_svc = MagicMock()
            svc._analytics_svc.handle_get_activity_calendar.return_value = {"weeks": []}
            self._dispatch(svc, "get_activity_calendar", {})
            svc._analytics_svc.handle_get_activity_calendar.assert_called_once()


# ---------------------------------------------------------------------------
# AnalyticsService registered in service.py source (grep test)
# ---------------------------------------------------------------------------

class TestAnalyticsServiceRegistration(unittest.TestCase):
    """Verify AnalyticsService is imported and wired in service.py."""

    def test_import_present_in_service_py(self) -> None:
        service_path = PROJECT_ROOT / "backend" / "service.py"
        content = service_path.read_text(encoding="utf-8")
        self.assertIn("from backend.analytics_service import AnalyticsService", content)

    def test_dispatch_entries_present(self) -> None:
        service_path = PROJECT_ROOT / "backend" / "service.py"
        content = service_path.read_text(encoding="utf-8")
        for method in (
            "get_analytics_dashboard",
            "get_sentiment_trends",
            "compare_periods",
            "get_keyword_cloud",
            "get_timeline_view",
            "get_activity_calendar",
        ):
            self.assertIn(f'"{method}"', content, f'"{method}" not found in service.py dispatch table')

    def test_stub_methods_present(self) -> None:
        """Backward-compat stub methods still exist in service.py."""
        service_path = PROJECT_ROOT / "backend" / "service.py"
        content = service_path.read_text(encoding="utf-8")
        for stub in (
            "_handle_compare_periods",
            "_handle_get_activity_calendar",
            "_handle_get_sentiment_trends",
            "_handle_get_keyword_cloud",
            "_handle_get_timeline_view",
            "_handle_get_analytics_dashboard",
        ):
            self.assertIn(stub, content, f"{stub} stub not found in service.py")
=======
        fake_report.recordings_change_pct = 0.0
        fake_report.duration_change_pct = 0.0
        fake_report.confidence_change = 0.0
        fake_report.new_languages = []
        fake_report.summary = "equal"

        with patch("backend.analytics_service._compare_periods_fn", return_value=fake_report):
            svc = _make_svc()
            result = svc.handle_compare_periods({
                "period1_start": "2026-01-01",
                "period1_end": "2026-01-31",
                "period2_start": "2026-02-01",
                "period2_end": "2026-02-28",
            })

        self.assertIn("period1", result)
        self.assertIn("period2", result)
        self.assertIn("summary", result)
        self.assertEqual(result["period1"]["recordings"], 3)


class TestHandleGetActivityCalendar(unittest.TestCase):

    def test_returns_calendar_dict(self):
        cal = MagicMock()
        fake_cal = MagicMock()
        fake_cal.to_dict.return_value = {"weeks": [], "total_recordings": 0}
        cal.generate_calendar.return_value = fake_cal
        svc = _make_svc(activity_calendar=cal, store=_FakeStore())
        result = svc.handle_get_activity_calendar({"months": 3})
        self.assertIn("weeks", result)

    def test_clamps_months_to_1_24(self):
        cal = MagicMock()
        fake_cal = MagicMock()
        fake_cal.to_dict.return_value = {}
        cal.generate_calendar.return_value = fake_cal
        svc = _make_svc(activity_calendar=cal, store=_FakeStore())
        svc.handle_get_activity_calendar({"months": 100})
        call_args = cal.generate_calendar.call_args
        self.assertEqual(call_args[1]["months"], 24)

    def test_include_svg_triggers_svg_generation(self):
        cal = MagicMock()
        fake_cal = MagicMock()
        fake_cal.to_dict.return_value = {}
        cal.generate_calendar.return_value = fake_cal
        cal.generate_calendar_svg.return_value = "<svg/>"
        svc = _make_svc(activity_calendar=cal, store=_FakeStore())
        result = svc.handle_get_activity_calendar({"include_svg": True})
        cal.generate_calendar_svg.assert_called_once()
        self.assertEqual(result["svg"], "<svg/>")

    def test_no_svg_by_default(self):
        cal = MagicMock()
        fake_cal = MagicMock()
        fake_cal.to_dict.return_value = {}
        cal.generate_calendar.return_value = fake_cal
        svc = _make_svc(activity_calendar=cal, store=_FakeStore())
        result = svc.handle_get_activity_calendar({})
        cal.generate_calendar_svg.assert_not_called()
        self.assertNotIn("svg", result)


class TestHandleGetRecordingInsights(unittest.TestCase):

    def test_returns_insights_list(self):
        ri = MagicMock()
        fake_insight = MagicMock()
        fake_insight.to_dict.return_value = {"type": "info", "message": "test"}
        ri.generate_insights.return_value = [fake_insight]
        svc = _make_svc(recording_insights=ri, store=_FakeStore())
        result = svc.handle_get_recording_insights({"days": 7})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["days"], 7)
        self.assertEqual(len(result["insights"]), 1)

    def test_empty_store_returns_empty_insights(self):
        ri = MagicMock()
        ri.generate_insights.return_value = []
        svc = _make_svc(recording_insights=ri, store=_FakeStore())
        result = svc.handle_get_recording_insights({})
        self.assertEqual(result["count"], 0)


class TestHandleGetSentimentTrends(unittest.TestCase):

    def test_delegates_to_sentiment_analyzer(self):
        st = MagicMock()
        fake_report = MagicMock()
        st.analyze_sentiment_trends.return_value = fake_report
        st.to_dict.return_value = {"trend": "stable", "daily": []}
        svc = _make_svc(sentiment_trends=st, store=_FakeStore())
        result = svc.handle_get_sentiment_trends({"days": 14})
        st.analyze_sentiment_trends.assert_called_once()
        st.to_dict.assert_called_once_with(fake_report)
        self.assertIn("trend", result)

    def test_default_days_is_30(self):
        st = MagicMock()
        st.analyze_sentiment_trends.return_value = MagicMock()
        st.to_dict.return_value = {}
        svc = _make_svc(sentiment_trends=st, store=_FakeStore())
        svc.handle_get_sentiment_trends({})
        call_kwargs = st.analyze_sentiment_trends.call_args[1]
        self.assertEqual(call_kwargs["days"], 30)


class TestHandleGetKeywordCloud(unittest.TestCase):

    def _make_word(self, word, count=5, weight=0.5, font_size=14):
        w = MagicMock()
        w.word = word
        w.count = count
        w.weight = weight
        w.font_size = font_size
        return w

    def test_returns_words_list(self):
        kg = MagicMock()
        kg.generate_cloud.return_value = [self._make_word("краб")]
        svc = _make_svc(keyword_cloud_gen=kg, store=_FakeStore())
        result = svc.handle_get_keyword_cloud({"max_words": 50})
        self.assertEqual(len(result["words"]), 1)
        self.assertEqual(result["words"][0]["word"], "краб")
        self.assertIn("count", result["words"][0])
        self.assertIn("weight", result["words"][0])
        self.assertIn("font_size", result["words"][0])

    def test_passes_language_param(self):
        kg = MagicMock()
        kg.generate_cloud.return_value = []
        svc = _make_svc(keyword_cloud_gen=kg, store=_FakeStore())
        svc.handle_get_keyword_cloud({"language": "ru"})
        call_kwargs = kg.generate_cloud.call_args[1]
        self.assertEqual(call_kwargs["language"], "ru")

    def test_default_max_words_100(self):
        kg = MagicMock()
        kg.generate_cloud.return_value = []
        svc = _make_svc(keyword_cloud_gen=kg, store=_FakeStore())
        svc.handle_get_keyword_cloud({})
        call_kwargs = kg.generate_cloud.call_args[1]
        self.assertEqual(call_kwargs["max_words"], 100)

    def test_empty_store_returns_empty_cloud(self):
        kg = MagicMock()
        kg.generate_cloud.return_value = []
        svc = _make_svc(keyword_cloud_gen=kg, store=_FakeStore())
        result = svc.handle_get_keyword_cloud({})
        self.assertEqual(result["words"], [])


class TestHandleGetMetricsDashboard(unittest.TestCase):

    def test_returns_expected_top_level_keys(self):
        svc = _make_svc()
        result = svc.handle_get_metrics_dashboard({})
        self.assertIn("session", result)
        self.assertIn("preview_loop", result)
        self.assertIn("llm", result)
        self.assertIn("call_assist", result)
        self.assertIn("config_snapshot", result)

    def test_settings_populate_config_snapshot(self):
        svc = _make_svc()
        result = svc.handle_get_metrics_dashboard({})
        snap = result["config_snapshot"]
        self.assertEqual(snap["quality"], "balanced")
        self.assertEqual(snap["translation_mode"], "off")

    def test_llm_model_from_settings(self):
        svc = _make_svc()
        result = svc.handle_get_metrics_dashboard({})
        self.assertEqual(result["llm"]["model"], "test-model")
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))


if __name__ == "__main__":
    unittest.main()
