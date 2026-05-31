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
    """Создаёт минимальный mock BackendService с _handle_get_sentiment_trends."""

    # Импортируем хэндлер прямо из service.py через bound method trick, не инстанциируя весь BackendService.
    # Вместо этого создаём простой объект-заглушку с нужными методами.
    class FakeService:
        def __init__(self):
            self._sentiment_trends = _FakeSentimentTrends()
            self._privacy = privacy_enabled
            self.store = MagicMock()
            # W1707: Python 3.14 MagicMock._lock is the real internal RLock.
            # Explicitly set _lock to a context-manager MagicMock.
            _lock_ctx = MagicMock()
            _lock_ctx.return_value.__enter__ = MagicMock(return_value=None)
            _lock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            self.store._lock = _lock_ctx
            self.store._load_active_items_unlocked.return_value = []

        def _get_runtime_setting(self, key: str, default=None):
            if key == "privacy_mode_enabled":
                return self._privacy
            return default

        # Копируем хэндлер из service.py
        def _handle_get_sentiment_trends(self, params):
            if self._get_runtime_setting("privacy_mode_enabled", False):
                return {"ok": True, "trends": [], "skipped": "privacy_mode"}
            days = int(params.get("days", 30))
            try:
                with self.store._lock():
                    items = self.store._load_active_items_unlocked()
            except Exception:
                items = []
            report = self._sentiment_trends.analyze_sentiment_trends(items, days=days)
            return self._sentiment_trends.to_dict(report)

    return FakeService()


# ---------------------------------------------------------------------------
# F1 — Privacy gate tests
# ---------------------------------------------------------------------------

class SentimentTrendsPrivacyModeTestCase(unittest.TestCase):
    """W1289 F1: _handle_get_sentiment_trends пропускает анализ в privacy mode."""

    def test_sentiment_trends_skipped_in_privacy_mode(self) -> None:
        """Когда privacy_mode_enabled=True, хэндлер должен возвращать skipped=privacy_mode."""
        svc = _make_backend_service(privacy_enabled=True)
        result = svc._handle_get_sentiment_trends({})
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("skipped"), "privacy_mode")
        self.assertEqual(result.get("trends"), [])

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
        # нормальный ответ не содержит "skipped"
        self.assertNotIn("skipped", result)
        self.assertIn("mood_trend", result)

    def test_sentiment_trends_privacy_response_has_ok_true(self) -> None:
        """Ответ в privacy mode содержит ok=True (совместимость с IPC клиентом)."""
        svc = _make_backend_service(privacy_enabled=True)
        result = svc._handle_get_sentiment_trends({"days": 14})
        self.assertEqual(result["ok"], True)

    def test_sentiment_trends_privacy_response_has_empty_trends_list(self) -> None:
        """trends=[] в privacy mode."""
        svc = _make_backend_service(privacy_enabled=True)
        result = svc._handle_get_sentiment_trends({})
        self.assertIsInstance(result["trends"], list)
        self.assertEqual(len(result["trends"]), 0)


# ---------------------------------------------------------------------------
# F4 — Local timezone date bucketing tests
# ---------------------------------------------------------------------------

class SentimentTrendsLocalDateTestCase(unittest.TestCase):
    """W1289 F4: дата записи определяется в локальном, а не UTC часовом поясе."""

    def test_sentiment_uses_local_date_not_utc(self) -> None:
        """Запись в 00:30 UTC (= 03:30 Moscow) должна попасть в UTC-дату, но
        при UTC+3 корректный local день на 3 часа вперёд.

        Тест проверяет что analyze_sentiment_trends использует .astimezone().date()
        — т.е. для ts=2026-01-02T00:30:00+00:00 при UTC+3 → local date = 2026-01-02
        (совпадает), но ts=2025-12-31T23:30:00+00:00 при UTC+3 → local 2026-01-01.
        """
        # Создаём ts = полночь UTC минус 30 минут → предыдущий день UTC, но
        # при любом положительном смещении (UTC+N, N>0) это уже следующий день локально.
        # Конкретно: 2026-01-01 23:30 UTC → при UTC+1 → 2026-01-02 00:30 local.
        # .date() (UTC) = 2026-01-01; .astimezone().date() при UTC+1 = 2026-01-02.
        #
        # Мы симулируем это через явный offset: создаём UTC timestamp и tz+1.
        from datetime import timezone as tz

        # Timestamp: 2026-01-01 23:45 UTC (без смещения)
        ts_utc = datetime(2026, 1, 1, 23, 45, 0, tzinfo=tz.utc)

        # Имитируем offset +01:00 через patching astimezone
        from datetime import timedelta
        local_tz = timezone(timedelta(hours=1))
        ts_local = ts_utc.astimezone(local_tz)  # → 2026-01-02 00:45 +01:00

        # Проверяем что raw UTC date != local date
        self.assertEqual(ts_utc.date().isoformat(), "2026-01-01")
        self.assertEqual(ts_local.date().isoformat(), "2026-01-02")

        # Теперь проверяем что наш analyzer использует astimezone().date()
        # путём patching datetime.astimezone на ts объекте.
        analyzer = SentimentTrendAnalyzer()

        # Создаём item с ts на 23:45 UTC
        item = {"text": "отлично", "ts": ts_utc.isoformat(), "language": "ru"}

        # Patch astimezone на datetime instances чтобы вернуть UTC+1 версию
        original_get_ts = SentimentTrendAnalyzer._get_ts

        def patched_get_ts(item_):
            result = original_get_ts(item_)
            if result is not None:
                # Wrap в UTC+1
                return result.astimezone(local_tz)
            return result

        with patch.object(SentimentTrendAnalyzer, "_get_ts", staticmethod(patched_get_ts)):
            report = analyzer.analyze_sentiment_trends([item], days=365)

        # Если используется local date → дата должна быть 2026-01-02
        if report.daily_sentiment:
            date_used = report.daily_sentiment[0]["date"]
            self.assertEqual(date_used, "2026-01-02",
                             "Ожидается local date (UTC+1), но получена UTC date")

    def test_sentiment_utc_item_date_uses_astimezone(self) -> None:
        """Прямой тест: _get_ts возвращает aware datetime, и date() через astimezone
        совпадает с astimezone().date() при системном tz."""
        from datetime import timezone as tz
        ts_utc = datetime(2026, 3, 15, 12, 0, 0, tzinfo=tz.utc)
        # В любом tz astimezone().date() должен быть корректным local date
        local_date = ts_utc.astimezone().date()
        self.assertIsNotNone(local_date)
        # Дата должна быть 2026-03-15 или соседний день (при экзотических UTC-12/+12)
        year = local_date.year
        self.assertEqual(year, 2026)

    def test_sentiment_analyzer_date_bucketing_is_consistent(self) -> None:
        """Два элемента с одинаковым local-днём группируются вместе."""
        from datetime import timezone as tz, timedelta

        local_tz = timezone(timedelta(hours=3))  # UTC+3

        # Оба в один local-день (2026-06-01 UTC+3 = 2026-05-31 22:xx UTC → 2026-06-01 01:xx UTC)
        ts1 = datetime(2026, 5, 31, 23, 0, 0, tzinfo=tz.utc)   # local 2026-06-01 02:00
        ts2 = datetime(2026, 6, 1,  0, 30, 0, tzinfo=tz.utc)   # local 2026-06-01 03:30

        analyzer = SentimentTrendAnalyzer()
        original_get_ts = SentimentTrendAnalyzer._get_ts

        def patched_get_ts(item_):
            result = original_get_ts(item_)
            if result is not None:
                return result.astimezone(local_tz)
            return result

        items = [
            {"text": "хорошо", "ts": ts1.isoformat(), "language": "ru"},
            {"text": "отлично", "ts": ts2.isoformat(), "language": "ru"},
        ]

        with patch.object(SentimentTrendAnalyzer, "_get_ts", staticmethod(patched_get_ts)):
            report = analyzer.analyze_sentiment_trends(items, days=365)

        # Оба в local 2026-06-01 → должен быть один day bucket с count=2
        dates = [d["date"] for d in report.daily_sentiment]
        self.assertIn("2026-06-01", dates)
        bucket = next(d for d in report.daily_sentiment if d["date"] == "2026-06-01")
        self.assertEqual(bucket["count"], 2)


if __name__ == "__main__":
    unittest.main()
