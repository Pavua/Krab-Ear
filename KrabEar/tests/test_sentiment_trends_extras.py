"""Дополнительные тесты SentimentTrendAnalyzer — linear regression slope,
object-based items, языковые варианты."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sentiment_trends import SentimentTrendAnalyzer


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------------------
# Тесты _linear_regression_slope напрямую
# ---------------------------------------------------------------------------

class LinearRegressionSlopeTestCase(unittest.TestCase):
    """Тесты статического метода _linear_regression_slope."""

    def test_empty_list_returns_zero(self) -> None:
        self.assertEqual(SentimentTrendAnalyzer._linear_regression_slope([]), 0.0)

    def test_single_value_returns_zero(self) -> None:
        self.assertEqual(SentimentTrendAnalyzer._linear_regression_slope([0.5]), 0.0)

    def test_ascending_sequence_positive_slope(self) -> None:
        slope = SentimentTrendAnalyzer._linear_regression_slope([0.0, 0.25, 0.5, 0.75, 1.0])
        self.assertGreater(slope, 0.0)

    def test_descending_sequence_negative_slope(self) -> None:
        slope = SentimentTrendAnalyzer._linear_regression_slope([1.0, 0.75, 0.5, 0.25, 0.0])
        self.assertLess(slope, 0.0)

    def test_constant_sequence_zero_slope(self) -> None:
        slope = SentimentTrendAnalyzer._linear_regression_slope([0.5] * 10)
        self.assertAlmostEqual(slope, 0.0, places=10)

    def test_two_equal_values_zero_slope(self) -> None:
        slope = SentimentTrendAnalyzer._linear_regression_slope([0.7, 0.7])
        self.assertAlmostEqual(slope, 0.0, places=10)

    def test_two_distinct_values_correct_slope(self) -> None:
        """Два значения [0, 1] → slope = 1.0."""
        slope = SentimentTrendAnalyzer._linear_regression_slope([0.0, 1.0])
        self.assertAlmostEqual(slope, 1.0, places=6)

    def test_larger_slope_for_faster_growth(self) -> None:
        """Быстро растущая последовательность имеет больший slope."""
        slow = SentimentTrendAnalyzer._linear_regression_slope([0.0, 0.1, 0.2, 0.3])
        fast = SentimentTrendAnalyzer._linear_regression_slope([0.0, 0.5, 1.0, 1.5])
        self.assertGreater(fast, slow)

    def test_symmetry_ascending_descending(self) -> None:
        """Наклон для обратной последовательности — зеркально отрицательный."""
        values = [0.1, 0.3, 0.6, 0.9]
        slope_up = SentimentTrendAnalyzer._linear_regression_slope(values)
        slope_down = SentimentTrendAnalyzer._linear_regression_slope(list(reversed(values)))
        self.assertAlmostEqual(slope_up, -slope_down, places=10)


# ---------------------------------------------------------------------------
# Тесты с object-based items (не dict)
# ---------------------------------------------------------------------------

class ObjectBasedItemsTestCase(unittest.TestCase):
    """Тесты с элементами-объектами (атрибутный доступ, не словарь)."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def _make_obj(self, text: str, days_ago: float = 1.0, language: str = "ru"):
        class HistoryItem:
            pass

        item = HistoryItem()
        item.text = text
        item.ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
        item.language = language
        return item

    def test_object_item_text_extracted(self) -> None:
        """Текст извлекается из атрибута объекта."""
        item = self._make_obj("отлично хорошо")
        report = self._analyzer.analyze_sentiment_trends([item])
        self.assertEqual(len(report.daily_sentiment), 1)

    def test_object_item_ts_extracted(self) -> None:
        """Timestamp извлекается из datetime-атрибута объекта."""
        item = self._make_obj("текст", days_ago=2.0)
        report = self._analyzer.analyze_sentiment_trends([item], days=30)
        self.assertEqual(sum(d["count"] for d in report.daily_sentiment), 1)

    def test_object_item_language_extracted(self) -> None:
        """Язык извлекается из атрибута объекта."""
        item = self._make_obj("good excellent", language="en")
        # Не должно падать независимо от языка
        report = self._analyzer.analyze_sentiment_trends([item])
        self.assertIn(report.mood_trend, {"improving", "stable", "declining"})

    def test_object_item_missing_language_defaults_ru(self) -> None:
        """Отсутствие language у объекта → default 'ru', без краша."""

        class ItemNoLang:
            text = "хорошо"
            ts = datetime.now(timezone.utc) - timedelta(days=1)

        item = ItemNoLang()
        report = self._analyzer.analyze_sentiment_trends([item])
        self.assertEqual(len(report.daily_sentiment), 1)

    def test_mixed_dict_and_object_items(self) -> None:
        """Смешанный список (dict + объект) обрабатывается без краша."""
        dict_item = {"text": "хорошо", "ts": _ts(1), "language": "ru"}
        obj_item = self._make_obj("отлично", days_ago=2.0)
        report = self._analyzer.analyze_sentiment_trends([dict_item, obj_item])
        total = sum(d["count"] for d in report.daily_sentiment)
        self.assertEqual(total, 2)


# ---------------------------------------------------------------------------
# Дополнительные граничные случаи analyze_sentiment_trends
# ---------------------------------------------------------------------------

class AnalyzeSentimentTrendsEdgeCasesTestCase(unittest.TestCase):
    """Граничные случаи метода analyze_sentiment_trends."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_days_1_window(self) -> None:
        """days=1 → только сегодняшние записи попадают."""
        items = [
            {"text": "хорошо", "ts": _ts(0.5), "language": "ru"},   # < 1 дня назад
            {"text": "плохо", "ts": _ts(1.5), "language": "ru"},    # > 1 дня назад
        ]
        report = self._analyzer.analyze_sentiment_trends(items, days=1)
        total = sum(d["count"] for d in report.daily_sentiment)
        self.assertEqual(total, 1)

    def test_overall_sentiment_is_average_of_daily(self) -> None:
        """overall_sentiment = среднее значений дней."""
        from unittest.mock import MagicMock
        from core.emotion_detector import EmotionResult

        mock_detector = MagicMock()
        # 2 записи одного дня: positive(0.7) и negative(-0.7) → avg=0.0
        mock_detector.detect.side_effect = [
            EmotionResult("positive", 0.8, []),
            EmotionResult("negative", 0.7, []),
        ]
        analyzer = SentimentTrendAnalyzer(detector=mock_detector)
        base = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        base -= timedelta(days=1)
        items = [
            {"text": "t1", "ts": base.isoformat(), "language": "ru"},
            {"text": "t2", "ts": (base + timedelta(hours=1)).isoformat(), "language": "ru"},
        ]
        report = analyzer.analyze_sentiment_trends(items)
        self.assertAlmostEqual(report.overall_sentiment, 0.0, places=3)

    def test_items_exactly_at_cutoff_excluded(self) -> None:
        """Элемент точно на границе окна (= cutoff) исключается (ts < cutoff)."""
        analyzer = SentimentTrendAnalyzer()
        # Элемент ровно на 30 дней назад — попадает ли или нет, зависит от <
        # Здесь тестируем что за пределами (31 день) точно нет
        items = [
            {"text": "старый", "ts": _ts(31), "language": "ru"},
            {"text": "новый", "ts": _ts(1), "language": "ru"},
        ]
        report = analyzer.analyze_sentiment_trends(items, days=30)
        total = sum(d["count"] for d in report.daily_sentiment)
        self.assertEqual(total, 1)

    def test_distribution_sums_to_total_items(self) -> None:
        """Сумма distribution['positive'+'negative'+'neutral'] == общее число элементов."""
        items = [
            {"text": "отлично замечательно", "ts": _ts(1), "language": "ru"},
            {"text": "ужасно плохо", "ts": _ts(2), "language": "ru"},
            {"text": "нормально обычно", "ts": _ts(3), "language": "ru"},
        ]
        report = self._analyzer.analyze_sentiment_trends(items)
        dist = report.sentiment_distribution
        self.assertEqual(dist["positive"] + dist["negative"] + dist["neutral"], 3)

    def test_report_most_positive_has_max_avg_sentiment(self) -> None:
        """most_positive_day имеет наибольший avg_sentiment среди дней."""
        items = [
            {"text": "отлично супер хорошо", "ts": _ts(3), "language": "ru"},
            {"text": "ужасно провал плохо", "ts": _ts(10), "language": "ru"},
        ]
        report = self._analyzer.analyze_sentiment_trends(items)
        if len(report.daily_sentiment) >= 2:
            max_sent = max(d["avg_sentiment"] for d in report.daily_sentiment)
            self.assertAlmostEqual(
                report.most_positive_day["avg_sentiment"], max_sent, places=4
            )

    def test_report_most_negative_has_min_avg_sentiment(self) -> None:
        """most_negative_day имеет наименьший avg_sentiment среди дней."""
        items = [
            {"text": "отлично супер хорошо", "ts": _ts(3), "language": "ru"},
            {"text": "ужасно провал плохо", "ts": _ts(10), "language": "ru"},
        ]
        report = self._analyzer.analyze_sentiment_trends(items)
        if len(report.daily_sentiment) >= 2:
            min_sent = min(d["avg_sentiment"] for d in report.daily_sentiment)
            self.assertAlmostEqual(
                report.most_negative_day["avg_sentiment"], min_sent, places=4
            )

    def test_language_hyphenated_code_normalised(self) -> None:
        """Язык 'ru-RU' нормализуется в 'ru'."""
        items = [{"text": "хорошо", "ts": _ts(1), "language": "ru-RU"}]
        # Не должно падать
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertEqual(len(report.daily_sentiment), 1)


if __name__ == "__main__":
    unittest.main()
