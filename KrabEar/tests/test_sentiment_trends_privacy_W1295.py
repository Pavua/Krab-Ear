"""Тесты W1295: privacy_mode gate и local-timezone date bucketing в sentiment_trends."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sentiment_trends import SentimentTrendAnalyzer
from backend.analytics_service import AnalyticsService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(text: str, days_ago: float = 1.0, language: str = "ru") -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"text": text, "ts": ts.isoformat(), "language": language}


class _FakeSentimentTrends:
    """Stub чтобы не поднимать настоящий SentimentTrendAnalyzer в сервисных тестах."""

    def analyze_sentiment_trends(self, items, days=30):
        return MagicMock()

    def to_dict(self, report):
        return {"daily_sentiment": [], "overall_sentiment": 0.0,
                "sentiment_distribution": {}, "mood_trend": "stable",
                "most_positive_day": {}, "most_negative_day": {}}


def _make_backend_service(privacy_enabled: bool):
    """Возвращает LIVE AnalyticsService с privacy gate, обёрнутый для совместимости
    со старым API теста (svc._handle_get_sentiment_trends).

    W#47: dead in-class BackendService._handle_get_sentiment_trends удалён —
    privacy gate теперь в extracted AnalyticsService. Тест проверяет ЖИВОЙ код.
    """
    store = MagicMock()
    # W1707: Python 3.14 MagicMock._lock is the real internal RLock.
    # Explicitly set _lock to a context-manager MagicMock.
    _lock_ctx = MagicMock()
    _lock_ctx.return_value.__enter__ = MagicMock(return_value=None)
    _lock_ctx.return_value.__exit__ = MagicMock(return_value=False)
    store._lock = _lock_ctx
    store._load_active_items_unlocked.return_value = []

    sentiment = _FakeSentimentTrends()
    analytics = AnalyticsService(
        analytics_dashboard=MagicMock(),
        sentiment_trends=sentiment,
        activity_calendar=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        timeline_view=MagicMock(),
        store=store,
        settings_get=lambda k, d=None: (
            privacy_enabled if k == "privacy_mode_enabled" else d
        ),
    )

    class _ServiceShim:
        """Adapts the live AnalyticsService to the test's historical API."""

        def __init__(self, svc):
            self._analytics = svc

        # Allow tests to swap the sentiment collaborator (privacy short-circuit check).
        @property
        def _sentiment_trends(self):
            return self._analytics._sentiment_trends

        @_sentiment_trends.setter
        def _sentiment_trends(self, value):
            self._analytics._sentiment_trends = value

        def _handle_get_sentiment_trends(self, params):
            return self._analytics.handle_get_sentiment_trends(params)

    return _ServiceShim(analytics)


# ---------------------------------------------------------------------------
# F1 — Privacy gate tests
# ---------------------------------------------------------------------------

class SentimentTrendsPrivacyModeTestCase(unittest.TestCase):
    """W1289 F1: _handle_get_sentiment_trends пропускает анализ в privacy mode."""

    def test_sentiment_trends_skipped_in_privacy_mode(self) -> None:
        """Когда privacy_mode_enabled=True, хэндлер должен короткозамкнуть анализ.

        W#47/W1369: живой privacy gate возвращает reason='privacy_mode_active' и
        пустой 'daily_sentiment' (схема совпадает с to_dict, не legacy 'trends')."""
        svc = _make_backend_service(privacy_enabled=True)
        result = svc._handle_get_sentiment_trends({})
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertEqual(result.get("daily_sentiment"), [])

    def test_sentiment_trends_skipped_does_not_call_detector(self) -> None:
        """В privacy mode EmotionDetector не должен вызываться."""
        svc = _make_backend_service(privacy_enabled=True)
        svc._sentiment_trends = MagicMock()
        svc._handle_get_sentiment_trends({})
        svc._sentiment_trends.analyze_sentiment_trends.assert_not_called()

    def test_sentiment_trends_runs_normally(self) -> None:
        """Когда privacy_mode_enabled=False, хэндлер должен вернуть нормальный ответ."""
        svc = _make_backend_service(privacy_enabled=False)
        result = svc._handle_get_sentiment_trends({"days": 7})
        # нормальный ответ не помечен privacy gate'ом
        self.assertNotIn("privacy_mode_active", result)
        self.assertIn("mood_trend", result)

    def test_sentiment_trends_privacy_response_has_ok_true(self) -> None:
        """Ответ в privacy mode содержит ok=True (совместимость с IPC клиентом)."""
        svc = _make_backend_service(privacy_enabled=True)
        result = svc._handle_get_sentiment_trends({"days": 14})
        self.assertEqual(result["ok"], True)

    def test_sentiment_trends_privacy_response_has_empty_trends_list(self) -> None:
        """daily_sentiment=[] в privacy mode (W1369 schema)."""
        svc = _make_backend_service(privacy_enabled=True)
        result = svc._handle_get_sentiment_trends({})
        self.assertIsInstance(result["daily_sentiment"], list)
        self.assertEqual(len(result["daily_sentiment"]), 0)


# ---------------------------------------------------------------------------
# F4 — Local timezone date bucketing tests
# ---------------------------------------------------------------------------

class SentimentTrendsLocalDateTestCase(unittest.TestCase):
    """W1289 F4: дата записи определяется в локальном, а не UTC часовом поясе.

    Часовой пояс процесса ФИКСИРУЕТСЯ (UTC+3) и восстанавливается после теста; на вход подаются
    чистые UTC-метки без подмены _get_ts — преобразование проверяет сам анализатор (раньше фикстура
    заранее переводила время, и анализатор с простым .date() получал ожидаемый день; к тому же весь
    класс пропускался на машинах/CI с UTC-часами).
    """

    def setUp(self) -> None:
        import os
        import time

        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset недоступен на этой платформе")
        old_tz = os.environ.get("TZ")

        def _restore() -> None:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

        self.addCleanup(_restore)
        os.environ["TZ"] = "Etc/GMT-3"  # POSIX-знак обратный: это UTC+3 без перехода на летнее время
        time.tzset()

    def _buckets(self, items):
        report = SentimentTrendAnalyzer().analyze_sentiment_trends(items, days=3650)
        self.assertTrue(report.daily_sentiment, "отчёт пуст — анализатор не принял записи")
        return {d["date"]: d["count"] for d in report.daily_sentiment}

    def test_sentiment_uses_local_date_not_utc(self) -> None:
        """23:45 UTC 1 января = 02:45 2 января по UTC+3 → bucket 2026-01-02, не 2026-01-01."""
        ts_utc = datetime(2026, 1, 1, 23, 45, 0, tzinfo=timezone.utc)
        buckets = self._buckets([{"text": "отлично", "ts": ts_utc.isoformat(), "language": "ru"}])
        self.assertEqual(buckets, {"2026-01-02": 1})

    def test_sentiment_utc_item_date_uses_astimezone(self) -> None:
        """22:30 UTC 15 марта = 01:30 16 марта по UTC+3: день сдвигается через astimezone()."""
        ts_utc = datetime(2026, 3, 15, 22, 30, 0, tzinfo=timezone.utc)
        buckets = self._buckets([{"text": "хорошо", "ts": ts_utc.isoformat(), "language": "ru"}])
        self.assertEqual(buckets, {"2026-03-16": 1})

    def test_sentiment_analyzer_date_bucketing_is_consistent(self) -> None:
        """Два элемента с одним local-днём (UTC+3) группируются вместе: один bucket, count=2."""
        ts1 = datetime(2026, 5, 31, 23, 0, 0, tzinfo=timezone.utc)   # local 2026-06-01 02:00
        ts2 = datetime(2026, 6, 1, 0, 30, 0, tzinfo=timezone.utc)    # local 2026-06-01 03:30
        buckets = self._buckets([
            {"text": "хорошо", "ts": ts1.isoformat(), "language": "ru"},
            {"text": "отлично", "ts": ts2.isoformat(), "language": "ru"},
        ])
        self.assertEqual(buckets, {"2026-06-01": 2})


if __name__ == "__main__":
    unittest.main()
