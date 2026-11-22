"""Тесты консистентности схемы ответа get_sentiment_trends при privacy_mode.

W1360 F2 MED: privacy_mode ответ возвращал "trends": [] вместо "daily_sentiment": []
что ломало Swift JSON decoder. Тесты проверяют что схема совпадает.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sentiment_trends import SentimentTrendAnalyzer, SentimentTrendReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(text: str, days_ago: float = 1.0, language: str = "ru") -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"text": text, "ts": ts.isoformat(), "language": language}


# W#47: the privacy gate now lives in the extracted AnalyticsService — the dead
# in-class _handle_get_sentiment_trends duplicate in service.py was deleted.
# Source-scraping tests below target the LIVE handler in analytics_service.py.
ANALYTICS_PY = PROJECT_ROOT / "backend" / "analytics_service.py"


class _TripwireStore:
    """Любое чтение истории — ошибка теста (privacy ON обязан вернуться до чтения)."""

    def __init__(self) -> None:
        self.lock_calls = 0
        self.read_calls = 0

    def _lock(self):
        self.lock_calls += 1
        raise AssertionError("store._lock() вызван при privacy ON")

    def _load_active_items_unlocked(self):
        self.read_calls += 1
        raise AssertionError("история прочитана при privacy ON")


# Независимый контракт privacy-ответа (то, что ждёт Swift-декодер), а не копия production-схемы.
_PRIVACY_CONTRACT = {
    "ok": bool,
    "daily_sentiment": list,
    "overall_sentiment": float,
    "mood_trend": str,
    "sentiment_distribution": dict,
    "most_positive_day": dict,
    "most_negative_day": dict,
    "reason": str,
}


def _privacy_response():
    """Настоящий AnalyticsService.handle_get_sentiment_trends при privacy ON."""
    from backend.analytics_service import AnalyticsService

    store = _TripwireStore()
    svc = AnalyticsService(
        analytics_dashboard=MagicMock(),
        sentiment_trends=SentimentTrendAnalyzer(),
        activity_calendar=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        timeline_view=MagicMock(),
        store=store,
        settings_get=lambda key, default=None: True if key == "privacy_mode_enabled" else default,
    )
    return svc.handle_get_sentiment_trends({}), store


class PrivacyResponseKeyTestCase(unittest.TestCase):
    """Фактический privacy-ответ реального обработчика соответствует независимому контракту."""

    def test_privacy_response_matches_contract_keys_and_types(self) -> None:
        resp, _ = _privacy_response()
        for key, typ in _PRIVACY_CONTRACT.items():
            self.assertIn(key, resp, f"в privacy-ответе нет обязательного ключа {key!r}")
            self.assertIsInstance(resp[key], typ, f"{key!r}: ожидается {typ.__name__}, получен {type(resp[key]).__name__}")
        self.assertNotIn("trends", resp, "устаревший ключ 'trends' (W1295)")

    def test_privacy_response_values(self) -> None:
        resp, _ = _privacy_response()
        self.assertIs(resp["ok"], True)
        self.assertEqual(resp["reason"], "privacy_mode_active")
        self.assertEqual(resp["daily_sentiment"], [])
        self.assertEqual(resp["sentiment_distribution"], {"positive": 0, "negative": 0, "neutral": 0})

    def test_privacy_response_does_not_read_history(self) -> None:
        resp, store = _privacy_response()
        self.assertEqual((store.lock_calls, store.read_calls), (0, 0))
        self.assertEqual(resp["reason"], "privacy_mode_active")


# ---------------------------------------------------------------------------
# TestCase 2: normal response schema unchanged
# ---------------------------------------------------------------------------

class NormalResponseSchemaTestCase(unittest.TestCase):
    """Проверяет что нормальный (не-privacy) ответ использует 'daily_sentiment'."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_normal_response_schema_unchanged(self) -> None:
        """to_dict() должен всегда возвращать ключ 'daily_sentiment'."""
        items = [_make_item("хорошо сегодня")]
        report = self._analyzer.analyze_sentiment_trends(items)
        result = self._analyzer.to_dict(report)

        self.assertIn("daily_sentiment", result,
                      "нормальный ответ должен содержать 'daily_sentiment'")
        self.assertNotIn("trends", result,
                         "нормальный ответ не должен содержать устаревший ключ 'trends'")

    def test_normal_response_has_mood_trend_key(self) -> None:
        """to_dict() должен содержать 'mood_trend' (не 'trend')."""
        items = [_make_item("отлично")]
        report = self._analyzer.analyze_sentiment_trends(items)
        result = self._analyzer.to_dict(report)

        self.assertIn("mood_trend", result,
                      "нормальный ответ должен содержать 'mood_trend'")
        self.assertNotIn("trend", [k for k in result if k != "mood_trend"],
                         "нет отдельного ключа 'trend' — только 'mood_trend'")

    def test_normal_response_full_schema(self) -> None:
        """to_dict() возвращает все ключи, ожидаемые Swift-декодером."""
        items = [_make_item("отличный день")]
        report = self._analyzer.analyze_sentiment_trends(items)
        result = self._analyzer.to_dict(report)

        expected_keys = {
            "daily_sentiment",
            "overall_sentiment",
            "sentiment_distribution",
            "mood_trend",
            "most_positive_day",
            "most_negative_day",
        }
        for key in expected_keys:
            self.assertIn(key, result, f"ключ '{key}' обязателен в схеме ответа")

    def test_empty_items_normal_response_schema(self) -> None:
        """Пустой список items → to_dict() тоже возвращает 'daily_sentiment'."""
        report = self._analyzer.analyze_sentiment_trends([])
        result = self._analyzer.to_dict(report)

        self.assertIn("daily_sentiment", result)
        self.assertIsInstance(result["daily_sentiment"], list)
        self.assertEqual(result["daily_sentiment"], [])


# ---------------------------------------------------------------------------
# TestCase 3/4: схема privacy-ответа совпадает со схемой to_dict() нормального ответа
# ---------------------------------------------------------------------------

class SchemaParityTestCase(unittest.TestCase):
    """Фактический privacy-ответ содержит все ключи нормального to_dict() (Swift-декодер)."""

    def test_privacy_gate_keys_cover_to_dict_keys(self) -> None:
        analyzer = SentimentTrendAnalyzer()
        normal_keys = set(analyzer.to_dict(analyzer.analyze_sentiment_trends([])).keys())
        resp, _ = _privacy_response()
        missing = normal_keys - set(resp)
        self.assertEqual(missing, set(), f"в privacy-ответе нет ключей нормальной схемы: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
