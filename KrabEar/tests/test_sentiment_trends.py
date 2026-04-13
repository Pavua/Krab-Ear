"""Unit-тесты для SentimentTrendAnalyzer."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sentiment_trends import SentimentTrendAnalyzer, SentimentTrendReport


def _make_item(text: str, days_ago: float = 1.0, language: str = "ru") -> dict:
    """Вспомогательная функция — создаёт элемент истории как словарь."""
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "text": text,
        "ts": ts.isoformat(),
        "language": language,
    }


class SentimentTrendReportStructureTestCase(unittest.TestCase):
    """Тесты структуры SentimentTrendReport."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_empty_items_returns_report_with_defaults(self) -> None:
        report = self._analyzer.analyze_sentiment_trends([])
        self.assertIsInstance(report, SentimentTrendReport)
        self.assertEqual(report.daily_sentiment, [])
        self.assertEqual(report.overall_sentiment, 0.0)
        self.assertEqual(report.mood_trend, "stable")
        self.assertEqual(report.most_positive_day, {})
        self.assertEqual(report.most_negative_day, {})

    def test_empty_returns_distribution_with_all_categories(self) -> None:
        report = self._analyzer.analyze_sentiment_trends([])
        self.assertIn("positive", report.sentiment_distribution)
        self.assertIn("negative", report.sentiment_distribution)
        self.assertIn("neutral", report.sentiment_distribution)

    def test_report_has_all_required_fields(self) -> None:
        items = [_make_item("отлично")]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertTrue(hasattr(report, "daily_sentiment"))
        self.assertTrue(hasattr(report, "overall_sentiment"))
        self.assertTrue(hasattr(report, "sentiment_distribution"))
        self.assertTrue(hasattr(report, "mood_trend"))
        self.assertTrue(hasattr(report, "most_positive_day"))
        self.assertTrue(hasattr(report, "most_negative_day"))


class SentimentScoringTestCase(unittest.TestCase):
    """Тесты корректности оценки тональности."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_positive_text_gives_positive_overall_sentiment(self) -> None:
        items = [
            _make_item("отлично! замечательно! спасибо большое"),
            _make_item("великолепно, всё хорошо"),
        ]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertGreater(report.overall_sentiment, 0.0)

    def test_negative_text_gives_negative_overall_sentiment(self) -> None:
        items = [
            _make_item("ужасно, провал, ошибка и катастрофа"),
            _make_item("плохо, неудача, надоело"),
        ]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertLess(report.overall_sentiment, 0.0)

    def test_overall_sentiment_in_valid_range(self) -> None:
        items = [
            _make_item("хорошо"),
            _make_item("плохо"),
            _make_item("нормально"),
        ]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertGreaterEqual(report.overall_sentiment, -1.0)
        self.assertLessEqual(report.overall_sentiment, 1.0)

    def test_distribution_counts_are_non_negative(self) -> None:
        items = [
            _make_item("отлично"),
            _make_item("ужасно"),
            _make_item("обычный текст без эмоций"),
        ]
        report = self._analyzer.analyze_sentiment_trends(items)
        for key, val in report.sentiment_distribution.items():
            self.assertGreaterEqual(val, 0, f"Отрицательное значение для '{key}'")


class DailySentimentAggregationTestCase(unittest.TestCase):
    """Тесты дневной агрегации тональности."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_single_day_produces_one_daily_entry(self) -> None:
        # Фиксируем оба элемента в пределах одной UTC-даты (noon ± 1ч),
        # чтобы исключить пересечение границы дня независимо от времени запуска.
        base = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        base = base - timedelta(days=2)
        ts1 = (base - timedelta(hours=1)).isoformat()
        ts2 = (base + timedelta(hours=1)).isoformat()
        items = [
            {"text": "хорошо", "ts": ts1, "language": "ru"},
            {"text": "отлично", "ts": ts2, "language": "ru"},
        ]
        report = self._analyzer.analyze_sentiment_trends(items, days=30)
        self.assertEqual(len(report.daily_sentiment), 1)

    def test_daily_entry_has_required_keys(self) -> None:
        items = [_make_item("отлично")]
        report = self._analyzer.analyze_sentiment_trends(items)
        entry = report.daily_sentiment[0]
        self.assertIn("date", entry)
        self.assertIn("avg_sentiment", entry)
        self.assertIn("dominant_emotion", entry)
        self.assertIn("count", entry)

    def test_items_outside_window_are_excluded(self) -> None:
        items = [
            _make_item("отлично", days_ago=5),    # внутри окна
            _make_item("хорошо", days_ago=40),    # за пределами 30-дневного окна
        ]
        report = self._analyzer.analyze_sentiment_trends(items, days=30)
        self.assertEqual(len(report.daily_sentiment), 1)

    def test_most_positive_and_negative_days_are_correct(self) -> None:
        # Разные дни: один позитивный, один негативный
        items = [
            _make_item("отлично замечательно супер", days_ago=3),
            _make_item("ужасно плохо провал катастрофа", days_ago=10),
        ]
        report = self._analyzer.analyze_sentiment_trends(items, days=30)
        self.assertGreater(
            report.most_positive_day["avg_sentiment"],
            report.most_negative_day["avg_sentiment"],
        )


class MoodTrendClassificationTestCase(unittest.TestCase):
    """Тесты классификации тренда настроения."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_mood_trend_is_valid_string(self) -> None:
        items = [_make_item("хорошо")]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertIn(report.mood_trend, {"improving", "stable", "declining"})

    def test_improving_trend_when_sentiment_rises(self) -> None:
        # Симулируем последовательно растущую тональность по дням
        base = datetime.now(timezone.utc)
        items = []
        positive_words = ["отлично", "замечательно", "супер", "великолепно", "шикарно"]
        for i, word in enumerate(positive_words):
            day_offset = len(positive_words) - i  # чем меньше days_ago, тем позитивнее
            ts = (base - timedelta(days=day_offset)).isoformat()
            # Добавляем больше позитивных слов для более поздних дней
            text = " ".join([word] * (i + 1))
            items.append({"text": text, "ts": ts, "language": "ru"})
        report = self._analyzer.analyze_sentiment_trends(items, days=30)
        # Тренд должен быть improving или stable (не declining)
        self.assertNotEqual(report.mood_trend, "declining")


class ToDictSerializationTestCase(unittest.TestCase):
    """Тесты сериализации SentimentTrendReport в dict."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_to_dict_returns_all_keys(self) -> None:
        items = [_make_item("отлично")]
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
        self.assertEqual(set(result.keys()), expected_keys)

    def test_to_dict_empty_report_is_json_serializable(self) -> None:
        import json
        report = self._analyzer.analyze_sentiment_trends([])
        result = self._analyzer.to_dict(report)
        # Не должно бросать исключение
        serialized = json.dumps(result)
        self.assertIsInstance(serialized, str)


class TimestampParsingTestCase(unittest.TestCase):
    """Тесты парсинга различных форматов timestamp."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_epoch_float_timestamp(self) -> None:
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
        items = [{"text": "хорошо", "ts": ts, "language": "ru"}]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertEqual(len(report.daily_sentiment), 1)

    def test_datetime_object_timestamp(self) -> None:
        ts = datetime.now(timezone.utc) - timedelta(days=1)
        items = [{"text": "отлично", "ts": ts, "language": "ru"}]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertEqual(len(report.daily_sentiment), 1)

    def test_items_without_ts_are_skipped(self) -> None:
        items = [{"text": "хорошо", "language": "ru"}]
        report = self._analyzer.analyze_sentiment_trends(items)
        self.assertEqual(report.daily_sentiment, [])

    def test_items_without_text_are_skipped(self) -> None:
        items = [_make_item(""), _make_item("отлично")]
        report = self._analyzer.analyze_sentiment_trends(items)
        # Только один элемент с текстом должен быть обработан
        self.assertEqual(sum(d["count"] for d in report.daily_sentiment), 1)


if __name__ == "__main__":
    unittest.main()
