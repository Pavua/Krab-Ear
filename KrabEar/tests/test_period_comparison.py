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


# ---------------------------------------------------------------------------
# Wave 93: additional required test cases
# ---------------------------------------------------------------------------

class CompareTwoFullPeriodsTestCase(unittest.TestCase):
    """test_compare_two_full_periods — basic delta between two populated periods."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()

    def test_basic_delta_recordings(self) -> None:
        """Recordings delta is computed correctly for two full periods."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [
                    {
                        "audio_duration_sec": 60.0,
                        "text": "один два три четыре пять",
                        "confidence": 0.88,
                        "source_lang": "RU",
                    }
                    for _ in range(4)
                ],
                None,
            ),
            (
                [
                    {
                        "audio_duration_sec": 90.0,
                        "text": "six seven eight",
                        "confidence": 0.92,
                        "source_lang": "EN",
                    }
                    for _ in range(8)
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-02-01", "2024-02-07",
            "2024-02-08", "2024-02-14",
        )

        self.assertEqual(report.period1.recordings, 4)
        self.assertEqual(report.period2.recordings, 8)
        # (8-4)/4*100 = 100.0
        self.assertAlmostEqual(report.recordings_change_pct, 100.0, places=1)

    def test_basic_delta_duration(self) -> None:
        """Duration delta is computed correctly."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [{"audio_duration_sec": 200.0, "text": "a b", "confidence": 0.8,
                  "source_lang": "RU"}],
                None,
            ),
            (
                [{"audio_duration_sec": 400.0, "text": "c d", "confidence": 0.8,
                  "source_lang": "RU"}],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-02-01", "2024-02-07",
            "2024-02-08", "2024-02-14",
        )

        self.assertAlmostEqual(report.duration_change_pct, 100.0, places=1)

    def test_basic_delta_confidence_change(self) -> None:
        """Confidence change (absolute) is computed and is a float."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [{"audio_duration_sec": 30.0, "text": "t", "confidence": 0.80,
                  "source_lang": "EN"}],
                None,
            ),
            (
                [{"audio_duration_sec": 30.0, "text": "t", "confidence": 0.90,
                  "source_lang": "EN"}],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-02-01", "2024-02-07",
            "2024-02-08", "2024-02-14",
        )

        self.assertAlmostEqual(report.confidence_change, 0.10, places=4)

    def test_new_languages_detected(self) -> None:
        """Languages present only in period2 appear in new_languages."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [{"audio_duration_sec": 30.0, "text": "t", "confidence": 0.80,
                  "source_lang": "RU"}],
                None,
            ),
            (
                [
                    {"audio_duration_sec": 30.0, "text": "t", "confidence": 0.80,
                     "source_lang": "RU"},
                    {"audio_duration_sec": 30.0, "text": "hola", "confidence": 0.80,
                     "source_lang": "ES"},
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-02-01", "2024-02-07",
            "2024-02-08", "2024-02-14",
        )

        self.assertIn("ES", report.new_languages)
        self.assertNotIn("RU", report.new_languages)

    def test_summary_contains_recording_counts(self) -> None:
        """Summary string includes both period recording counts."""
        self.mock_store.get_history_page_filtered.side_effect = [
            (
                [{"audio_duration_sec": 30.0, "text": "a", "confidence": 0.8,
                  "source_lang": "RU"}
                 for _ in range(3)],
                None,
            ),
            (
                [{"audio_duration_sec": 30.0, "text": "a", "confidence": 0.8,
                  "source_lang": "RU"}
                 for _ in range(6)],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-02-01", "2024-02-07",
            "2024-02-08", "2024-02-14",
        )

        self.assertIn("3", report.summary)
        self.assertIn("6", report.summary)


class OverlappingPeriodsTestCase(unittest.TestCase):
    """test_overlapping_periods — overlapping date ranges produce a report
    (source has no validation guard; document that behavior here so any future
    guard addition will be caught by a failing test that must be updated)."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()
        self.mock_store.get_history_page_filtered.return_value = ([], None)
        self.service = PeriodComparisonService(self.mock_store)

    def test_overlapping_periods_do_not_raise(self) -> None:
        """Overlapping custom periods are accepted without raising — store is queried twice."""
        # period1: Jan 1-10, period2: Jan 5-15 (overlap Jan 5-10)
        result = self.service.handle_compare_periods({
            "period1_start": "2024-01-01",
            "period1_end": "2024-01-10",
            "period2_start": "2024-01-05",
            "period2_end": "2024-01-15",
        })
        self.assertIsInstance(result, dict)
        self.assertIn("period1", result)

    def test_overlapping_periods_calls_store_twice(self) -> None:
        """Store is queried exactly twice (once per period) even when they overlap."""
        self.service.handle_compare_periods({
            "period1_start": "2024-01-01",
            "period1_end": "2024-01-10",
            "period2_start": "2024-01-05",
            "period2_end": "2024-01-15",
        })
        self.assertEqual(self.mock_store.get_history_page_filtered.call_count, 2)

    def test_identical_periods_treated_as_same(self) -> None:
        """Identical periods (full overlap) return zero delta."""
        data = [
            {"audio_duration_sec": 60.0, "text": "same", "confidence": 0.85,
             "source_lang": "RU"},
        ]
        self.mock_store.get_history_page_filtered.side_effect = [
            (data, None),
            (data, None),
        ]

        result = self.service.handle_compare_periods({
            "period1_start": "2024-01-01",
            "period1_end": "2024-01-07",
            "period2_start": "2024-01-01",
            "period2_end": "2024-01-07",
        })

        self.assertEqual(result["recordings_change_pct"], 0.0)


class PeriodUnitDayWeekMonthTestCase(unittest.TestCase):
    """test_period_unit_day_week_month — service modes dispatch correctly."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()
        self.mock_store.get_history_page_filtered.return_value = ([], None)
        self.service = PeriodComparisonService(self.mock_store)

    def test_custom_mode_uses_provided_dates(self) -> None:
        """mode='custom' (default) passes supplied dates to store."""
        self.service.handle_compare_periods({
            "period1_start": "2024-03-01",
            "period1_end": "2024-03-07",
            "period2_start": "2024-03-08",
            "period2_end": "2024-03-14",
        })
        # store must be called exactly twice (no pagination)
        self.assertEqual(self.mock_store.get_history_page_filtered.call_count, 2)
        # Verify the from_ts of first call contains period1_start date
        first_call_kwargs = self.mock_store.get_history_page_filtered.call_args_list[0][1]
        self.assertIn("2024-03-01", first_call_kwargs["from_ts"])

    def test_weeks_mode_returns_valid_dict(self) -> None:
        """mode='weeks' produces a valid ComparisonReport dict."""
        result = self.service.handle_compare_periods({"mode": "weeks", "weeks_back": 2})
        self.assertIsInstance(result, dict)
        required_keys = {"period1", "period2", "recordings_change_pct",
                         "duration_change_pct", "confidence_change",
                         "new_languages", "summary"}
        self.assertTrue(required_keys.issubset(result.keys()))

    def test_months_mode_returns_valid_dict(self) -> None:
        """mode='months' produces a valid ComparisonReport dict."""
        result = self.service.handle_compare_periods({"mode": "months"})
        self.assertIsInstance(result, dict)
        self.assertIn("summary", result)

    def test_weeks_mode_calls_store_twice(self) -> None:
        """mode='weeks' queries store twice (one per period)."""
        self.service.handle_compare_periods({"mode": "weeks"})
        self.assertEqual(self.mock_store.get_history_page_filtered.call_count, 2)

    def test_months_mode_calls_store_twice(self) -> None:
        """mode='months' queries store twice."""
        self.service.handle_compare_periods({"mode": "months"})
        self.assertEqual(self.mock_store.get_history_page_filtered.call_count, 2)

    def test_missing_dates_in_custom_mode_raises_value_error(self) -> None:
        """mode='custom' without required date params raises ValueError."""
        with self.assertRaises(ValueError):
            self.service.handle_compare_periods({"mode": "custom"})


class PercentageDeltaZeroBaselineTestCase(unittest.TestCase):
    """test_percentage_delta_zero_baseline — divide-by-zero guard in _pct_change."""

    def test_zero_old_returns_zero(self) -> None:
        """_pct_change(0, any) == 0.0 (no ZeroDivisionError)."""
        self.assertEqual(_pct_change(0.0, 500.0), 0.0)

    def test_zero_old_negative_new_returns_zero(self) -> None:
        """_pct_change(0, negative) also returns 0.0."""
        self.assertEqual(_pct_change(0.0, -100.0), 0.0)

    def test_zero_old_zero_new_returns_zero(self) -> None:
        """_pct_change(0, 0) == 0.0."""
        self.assertEqual(_pct_change(0.0, 0.0), 0.0)

    def test_period_with_zero_recordings_zero_duration_no_crash(self) -> None:
        """compare_periods with both periods empty returns zero pct changes."""
        mock_store = MagicMock()
        mock_store.get_history_page_filtered.return_value = ([], None)

        report = compare_periods(
            mock_store,
            "2024-01-01", "2024-01-07",
            "2024-01-08", "2024-01-14",
        )

        self.assertEqual(report.recordings_change_pct, 0.0)
        self.assertEqual(report.duration_change_pct, 0.0)
        self.assertEqual(report.confidence_change, 0.0)

    def test_duration_zero_baseline_no_crash(self) -> None:
        """Period1 has items with zero duration — no ZeroDivisionError for duration pct."""
        mock_store = MagicMock()
        mock_store.get_history_page_filtered.side_effect = [
            (
                [{"audio_duration_sec": 0.0, "text": "test", "confidence": 0.8,
                  "source_lang": "RU"}],
                None,
            ),
            (
                [{"audio_duration_sec": 100.0, "text": "test", "confidence": 0.8,
                  "source_lang": "RU"}],
                None,
            ),
        ]

        report = compare_periods(
            mock_store,
            "2024-01-01", "2024-01-07",
            "2024-01-08", "2024-01-14",
        )

        self.assertEqual(report.duration_change_pct, 0.0)


class UnicodeSummaryTextTestCase(unittest.TestCase):
    """test_unicode_summary_text — Cyrillic in summary and language names."""

    def setUp(self) -> None:
        self.mock_store = MagicMock()

    def test_summary_contains_cyrillic(self) -> None:
        """Summary для русскоязычных данных содержит кириллические символы."""
        self.mock_store.get_history_page_filtered.return_value = ([], None)

        report = compare_periods(
            self.mock_store,
            "2024-05-01", "2024-05-07",
            "2024-05-08", "2024-05-14",
        )

        # Default summary for empty periods includes "Записей:" — Cyrillic word
        self.assertIn("Записей", report.summary)

    def test_summary_is_valid_string(self) -> None:
        """Summary поле — всегда строка, никогда bytes."""
        self.mock_store.get_history_page_filtered.return_value = ([], None)

        report = compare_periods(
            self.mock_store,
            "2024-05-01", "2024-05-07",
            "2024-05-08", "2024-05-14",
        )

        self.assertIsInstance(report.summary, str)

    def test_new_languages_with_unicode_lang_codes(self) -> None:
        """Языковые коды с unicode-значениями корректно попадают в new_languages."""
        self.mock_store.get_history_page_filtered.side_effect = [
            ([], None),
            (
                [
                    {"audio_duration_sec": 10.0, "text": "тест", "confidence": 0.9,
                     "source_lang": "RU"},
                ],
                None,
            ),
        ]

        report = compare_periods(
            self.mock_store,
            "2024-05-01", "2024-05-07",
            "2024-05-08", "2024-05-14",
        )

        self.assertIn("RU", report.new_languages)

    def test_report_to_dict_unicode_safe(self) -> None:
        """_report_to_dict сериализует summary с кириллицей без ошибок."""
        from backend.period_comparison import _report_to_dict, ComparisonReport, PeriodStats
        import json

        stats = PeriodStats(
            recordings=5,
            duration_sec=300.0,
            words=100,
            avg_confidence=0.85,
            languages=["RU", "ES"],
        )
        report = ComparisonReport(
            period1=stats,
            period2=stats,
            recordings_change_pct=0.0,
            duration_change_pct=0.0,
            confidence_change=0.0,
            new_languages=[],
            summary="Записей: 5 (было 5, +0.0%); Длительность: 300s (было 300s, +0.0%)",
        )

        d = _report_to_dict(report)
        # Must be JSON-serializable with Cyrillic
        serialized = json.dumps(d, ensure_ascii=False)
        self.assertIn("Записей", serialized)


if __name__ == "__main__":
    unittest.main()
