"""Тесты compare_periods — сравнение периодов использования Krab Ear."""

from __future__ import annotations
from backend.period_comparison import (
    compare_periods,
    PeriodStats,
    PeriodComparisonService,
    _pct_change,
)

from pathlib import Path
from unittest.mock import MagicMock
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class PercentageChangeTestCase(unittest.TestCase):
    """Тесты функции _pct_change."""

    def test_pct_change_zero_baseline(self) -> None:
        """old == 0 → результат 0 (деление на 0 защищено)."""
        result = _pct_change(0.0, 100.0)
        self.assertEqual(result, 0.0)

    def test_pct_change_positive_growth(self) -> None:
        """new > old → положительное значение."""
        result = _pct_change(100.0, 150.0)
        self.assertAlmostEqual(result, 50.0, places=1)

    def test_pct_change_negative_decline(self) -> None:
        """new < old → отрицательное значение."""
        result = _pct_change(100.0, 50.0)
        self.assertAlmostEqual(result, -50.0, places=1)

    def test_pct_change_same_values(self) -> None:
        """new == old → результат 0."""
        result = _pct_change(100.0, 100.0)
        self.assertEqual(result, 0.0)

    def test_pct_change_rounding(self) -> None:
        """Результат округляется до 2 знаков после запятой."""
        result = _pct_change(3.0, 10.0)
        self.assertEqual(result, 233.33)


class PeriodComparisonTwoPeriodsTestCase(unittest.TestCase):
    """Тесты сравнения двух периодов с данными."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()

    def test_compare_week1_vs_week2_with_data(self) -> None:
        """Сравнение двух недель с разными счётчиками."""
        # Period 1: 10 записей, 3600 сек, 500 слов, 0.92 confidence, EN+RU
        # Period 2: 15 записей, 4200 сек, 700 слов, 0.95 confidence, EN+RU+ES
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [
                    {
                        "audio_duration_sec": 360,
                        "text": "word " * 50,
                        "confidence": 0.92,
                        "source_lang": "EN",
                    }
                    for _ in range(10)
                ],
                None,
            ),
            (
                [
                    {
                        "audio_duration_sec": 280,
                        "text": "слово " * 47,
                        "confidence": 0.95,
                        "source_lang": "ES",
                    }
                    for _ in range(5)
                ]
                + [
                    {
                        "audio_duration_sec": 280,
                        "text": "слово " * 47,
                        "confidence": 0.95,
                        "source_lang": "RU",
                    }
                    for _ in range(10)
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertEqual(report.period1.recordings, 10)
        self.assertEqual(report.period2.recordings, 15)
        self.assertAlmostEqual(report.recordings_change_pct, 50.0, places=0)
        self.assertIn("ES", report.new_languages)

    def test_compare_periods_empty_to_full(self) -> None:
        """Переход от пустого периода к периоду с данными."""
        self.mock_store.get_history_page_filtered.side_effect = [
            ([], None),
            (
                [
                    {
                        "audio_duration_sec": 100,
                        "text": "test words",
                        "confidence": 0.90,
                        "source_lang": "RU",
                    }
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertEqual(report.period1.recordings, 0)
        self.assertEqual(report.period2.recordings, 1)
        self.assertEqual(report.recordings_change_pct, 0.0)

    def test_compare_periods_percentage_accuracy(self) -> None:
        """Точность расчёта процентных изменений для countable метрик."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [
                    {"audio_duration_sec": 100, "text": "w", "confidence": 0.8,
                     "source_lang": "RU"}
                ],
                None,
            ),
            (
                [
                    {"audio_duration_sec": 200, "text": "w", "confidence": 0.8,
                     "source_lang": "RU"}
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertAlmostEqual(report.duration_change_pct, 100.0, places=0)


class EmptyPeriodTestCase(unittest.TestCase):
    """Тесты обработки пустых периодов."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()

    def test_both_periods_empty(self) -> None:
        """Обе недели без данных → нулевые счётчики."""
        self.mock_store.get_history_page_filtered.return_value = ([], None)

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertEqual(report.period1.recordings, 0)
        self.assertEqual(report.period2.recordings, 0)
        self.assertEqual(report.recordings_change_pct, 0.0)
        self.assertEqual(report.duration_change_pct, 0.0)
        self.assertEqual(report.confidence_change, 0.0)

    def test_empty_period_returns_zero_stats(self) -> None:
        """Пустой период возвращает PeriodStats с нулями."""
        self.mock_store.get_history_page_filtered.return_value = ([], None)

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        stats = report.period1
        self.assertEqual(stats.recordings, 0)
        self.assertEqual(stats.duration_sec, 0.0)
        self.assertEqual(stats.words, 0)
        self.assertEqual(stats.avg_confidence, 0.0)
        self.assertEqual(stats.languages, [])


class SinglePeriodEdgeCaseTestCase(unittest.TestCase):
    """Тесты граничных случаев."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()

    def test_single_recording_in_period(self) -> None:
        """Единственная запись в периоде."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [
                    {
                        "audio_duration_sec": 60.0,
                        "text": "hello world",
                        "confidence": 0.95,
                        "source_lang": "EN",
                    }
                ],
                None,
            ),
            ([], None),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertEqual(report.period1.recordings, 1)
        self.assertEqual(report.period2.recordings, 0)

    def test_no_confidence_data(self) -> None:
        """Записи без confidence → avg_confidence == 0."""
        self.mock_store.get_history_page_filtered.return_value = (
            [
                {
                    "audio_duration_sec": 100,
                    "text": "test",
                    "confidence": None,
                    "source_lang": "RU",
                }
            ],
            None,
        )

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertEqual(report.period1.avg_confidence, 0.0)


class PersistenceFreeTestCase(unittest.TestCase):
    """Тесты что сравнение не использует persistent storage."""

    def test_no_side_effects_on_store(self) -> None:
        """compare_periods не модифицирует хранилище."""
        mock_store = MagicMock()
        mock_store.get_history_page_filtered.return_value = ([], None)

        compare_periods(
            mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        # Проверяем что были вызовы get_history_page_filtered
        self.assertGreater(mock_store.get_history_page_filtered.call_count, 0)
        # Но нет write/modify операций
        self.assertFalse(mock_store.add_history_item.called)


class PercentageRoundingTestCase(unittest.TestCase):
    """Тесты правильности округления процентов."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()

    def test_confidence_change_rounding(self) -> None:
        """Изменение confidence округляется до 4 знаков."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [
                    {
                        "audio_duration_sec": 100,
                        "text": "test",
                        "confidence": 0.8123,
                        "source_lang": "RU",
                    }
                ],
                None,
            ),
            (
                [
                    {
                        "audio_duration_sec": 100,
                        "text": "test",
                        "confidence": 0.8567,
                        "source_lang": "RU",
                    }
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        expected_change = 0.0444
        self.assertAlmostEqual(report.confidence_change, expected_change, places=4)

    def test_pct_change_values_rounded_to_two_decimals(self) -> None:
        """Процентные изменения округляются до 2 знаков."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [
                    {
                        "audio_duration_sec": 100,
                        "text": "t",
                        "confidence": 0.9,
                        "source_lang": "RU",
                    }
                    for _ in range(3)
                ],
                None,
            ),
            (
                [
                    {
                        "audio_duration_sec": 100,
                        "text": "t",
                        "confidence": 0.9,
                        "source_lang": "RU",
                    }
                    for _ in range(7)
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        # 7 vs 3 → (7-3)/3*100 = 133.33
        expected = 133.33
        self.assertEqual(report.recordings_change_pct, expected)


class ComparisonReportStructureTestCase(unittest.TestCase):
    """Тесты структуры ComparisonReport."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()
        self.mock_store.get_history_page_filtered.return_value = ([], None)

    def test_report_has_all_required_fields(self) -> None:
        """ComparisonReport содержит все обязательные поля."""
        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertIsInstance(report.period1, PeriodStats)
        self.assertIsInstance(report.period2, PeriodStats)
        self.assertIsInstance(report.recordings_change_pct, float)
        self.assertIsInstance(report.duration_change_pct, float)
        self.assertIsInstance(report.confidence_change, float)
        self.assertIsInstance(report.new_languages, list)
        self.assertIsInstance(report.summary, str)

    def test_summary_non_empty(self) -> None:
        """Summary-поле не пустое."""
        report = compare_periods(
            self.mock_store,
            "2024-01-01",
            "2024-01-07",
            "2024-01-08",
            "2024-01-14",
        )

        self.assertGreater(len(report.summary), 0)


class SamePeriodZeroDeltaTestCase(unittest.TestCase):
    """Тесты сравнения одинакового периода с самим собой → нулевые дельты."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()

    def _same_data(self) -> list[dict]:
        return [
            {
                "audio_duration_sec": 120.0,
                "text": "один два три",
                "confidence": 0.88,
                "source_lang": "RU",
            }
            for _ in range(5)
        ]

    def test_same_period_zero_recordings_delta(self) -> None:
        """Одинаковые периоды → recordings_change_pct == 0.0."""
        data = self._same_data()
        self.mock_store.get_history_page_filtered.side_effect = [
            (data, None),
            (data, None),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-03-01",
            "2024-03-07",
            "2024-03-01",
            "2024-03-07",
        )

        self.assertEqual(report.recordings_change_pct, 0.0)

    def test_same_period_zero_duration_delta(self) -> None:
        """Одинаковые периоды → duration_change_pct == 0.0."""
        data = self._same_data()
        self.mock_store.get_history_page_filtered.side_effect = [
            (data, None),
            (data, None),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-03-01",
            "2024-03-07",
            "2024-03-01",
            "2024-03-07",
        )

        self.assertEqual(report.duration_change_pct, 0.0)

    def test_same_period_zero_confidence_delta(self) -> None:
        """Одинаковые периоды → confidence_change == 0.0."""
        data = self._same_data()
        self.mock_store.get_history_page_filtered.side_effect = [
            (data, None),
            (data, None),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-03-01",
            "2024-03-07",
            "2024-03-01",
            "2024-03-07",
        )

        self.assertEqual(report.confidence_change, 0.0)

    def test_same_period_no_new_languages(self) -> None:
        """Одинаковые периоды → нет новых языков."""
        data = self._same_data()
        self.mock_store.get_history_page_filtered.side_effect = [
            (data, None),
            (data, None),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-03-01",
            "2024-03-07",
            "2024-03-01",
            "2024-03-07",
        )

        self.assertEqual(report.new_languages, [])


class PeriodComparisonServiceTestCase(unittest.TestCase):
    """Тесты PeriodComparisonService IPC-обёртки."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()
        self.service = PeriodComparisonService(self.mock_store)

    def test_service_compare_periods_handler(self) -> None:
        """handle_compare_periods возвращает dict."""
        self.mock_store.get_history_page_filtered.return_value = ([], None)

        result = self.service.handle_compare_periods({
            "period1_start": "2024-01-01",
            "period1_end": "2024-01-07",
            "period2_start": "2024-01-08",
            "period2_end": "2024-01-14",
        })

        self.assertIsInstance(result, dict)
        self.assertIn("period1", result)
        self.assertIn("period2", result)
        self.assertIn("summary", result)

    def test_service_weeks_mode(self) -> None:
        """Service обрабатывает mode='weeks'."""
        self.mock_store.get_history_page_filtered.return_value = ([], None)

        result = self.service.handle_compare_periods({
            "mode": "weeks",
            "weeks_back": 2,
        })

        self.assertIsInstance(result, dict)
        self.assertIn("summary", result)

    def test_service_months_mode(self) -> None:
        """Service обрабатывает mode='months'."""
        self.mock_store.get_history_page_filtered.return_value = ([], None)

        result = self.service.handle_compare_periods({
            "mode": "months",
        })

        self.assertIsInstance(result, dict)
        self.assertIn("summary", result)


if __name__ == "__main__":
    unittest.main()
