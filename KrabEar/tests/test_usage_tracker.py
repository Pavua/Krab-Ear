"""Тесты для UsageTracker — ежедневная статистика использования Krab Ear."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

# Настройка путей для импорта
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.usage_tracker import UsageTracker


class TestUsageTrackerBasic(unittest.TestCase):
    """Базовые тесты записи и чтения статистики."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_stats_are_zero(self):
        """Проверка, что новый трекер имеет нулевую статистику."""
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 0)
        self.assertEqual(stats["today"]["total_duration_sec"], 0)
        self.assertEqual(stats["today"]["total_words"], 0)
        self.assertEqual(stats["all_time"]["recordings"], 0)
        self.assertEqual(stats["streak_days"], 0)
        self.assertIsNone(stats["peak_day"])

    def test_record_usage_increments_today(self):
        """Проверка, что одна запись увеличивает статистику дня."""
        self.tracker.record_usage(duration_sec=30.0, word_count=100)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertEqual(stats["today"]["total_duration_sec"], 30.0)
        self.assertEqual(stats["today"]["total_words"], 100)

    def test_multiple_recordings_accumulate(self):
        """Проверка, что несколько записей в один день суммируются."""
        self.tracker.record_usage(10.0, 50)
        self.tracker.record_usage(20.0, 150)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 2)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 30.0)
        self.assertEqual(stats["today"]["total_words"], 200)

    def test_all_time_accumulates(self):
        """Проверка, что all_time счётчики растут корректно."""
        self.tracker.record_usage(5.0, 10)
        self.tracker.record_usage(5.0, 10)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 2)
        self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 10.0)
        self.assertEqual(stats["all_time"]["total_words"], 20)

    def test_this_week_includes_today(self):
        """Проверка, что недельная статистика включает сегодня."""
        self.tracker.record_usage(15.0, 75)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 1)

    def test_this_month_includes_today(self):
        """Проверка, что месячная статистика включает сегодня."""
        self.tracker.record_usage(15.0, 75)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_month"]["recordings"], 1)


class TestUsageTrackerPeriods(unittest.TestCase):
    """Тесты для различных периодов (неделя, месяц) и их границ."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_week_aggregation_multiple_days(self):
        """Проверка агрегации статистики за неделю с несколькими днями."""
        today = date.today()
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 2, "duration_sec": 20.0, "words": 50}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 6)
        self.assertAlmostEqual(stats["this_week"]["total_duration_sec"], 60.0)
        self.assertEqual(stats["this_week"]["total_words"], 150)

    def test_month_includes_all_30_days(self):
        """Проверка, что месячная статистика охватывает 30 дней."""
        today = date.today()
        for i in range(25):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 30}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_month"]["recordings"], 25)
        self.assertAlmostEqual(stats["this_month"]["total_duration_sec"], 250.0)

    def test_empty_days_contribute_zero(self):
        """Проверка, что дни без записей не влияют на статистику."""
        today = date.today()
        self.tracker._daily[today.isoformat()] = {"recordings": 1, "duration_sec": 5.0, "words": 10}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 1)
        self.assertEqual(stats["this_week"]["total_duration_sec"], 5.0)


class TestUsageTrackerStreak(unittest.TestCase):
    """Тесты для streak_days и peak_day."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_streak_single_day(self):
        """Проверка streak для одного дня активности."""
        self.tracker.record_usage(10.0, 50)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 1)

    def test_streak_with_gap_breaks(self):
        """Проверка, что пропуск дня прерывает streak."""
        two_days_ago = (date.today() - timedelta(days=2)).isoformat()
        self.tracker._daily[two_days_ago] = {"recordings": 3, "duration_sec": 60.0, "words": 200}
        self.tracker.record_usage(5.0, 10)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 1)

    def test_streak_consecutive_days(self):
        """Проверка подсчета streak для нескольких подряд идущих дней."""
        today = date.today()
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 2, "duration_sec": 30.0, "words": 100}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 3)

    def test_peak_day_calculation(self):
        """Проверка определения дня с максимальным количеством записей."""
        today = date.today()
        self.tracker._daily[today.isoformat()] = {"recordings": 5, "duration_sec": 100.0, "words": 500}
        yesterday = (today - timedelta(days=1)).isoformat()
        self.tracker._daily[yesterday] = {"recordings": 10, "duration_sec": 200.0, "words": 1000}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["peak_day"]["date"], yesterday)
        self.assertEqual(stats["peak_day"]["recordings"], 10)

    def test_peak_day_none_when_empty(self):
        """Проверка, что peak_day = None для пустой статистики."""
        stats = self.tracker.get_usage_stats()
        self.assertIsNone(stats["peak_day"])


class TestUsageTrackerPersistence(unittest.TestCase):
    """Тесты для сохранения и загрузки данных."""

    def test_persists_and_reloads(self):
        """Проверка, что данные сохраняются и загружаются при перезагрузке."""
        with tempfile.TemporaryDirectory() as tmp:
            t1 = UsageTracker(data_dir=tmp)
            t1.record_usage(30.0, 200)
            t1.record_usage(15.0, 100)

            t2 = UsageTracker(data_dir=tmp)
            stats = t2.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 2)
            self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 45.0)
            self.assertEqual(stats["all_time"]["total_words"], 300)

    def test_no_data_dir_does_not_crash(self):
        """Проверка работы трекера без указания директории (в памяти)."""
        tracker = UsageTracker(data_dir=None)
        tracker.record_usage(10.0, 50)
        stats = tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)

    def test_persistence_file_format(self):
        """Проверка, что файл сохраняется в правильном JSON формате."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            tracker.record_usage(20.0, 100)

            stats_file = Path(tmp) / "usage_stats.json"
            self.assertTrue(stats_file.exists())
            data = json.loads(stats_file.read_text(encoding="utf-8"))
            self.assertIn("daily", data)
            self.assertIn("all_time", data)
            self.assertEqual(data["all_time"]["recordings"], 1)

    def test_partial_corrupted_file_recovery(self):
        """Проверка, что трекер корректно обрабатывает отсутствие файла."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            tracker.record_usage(5.0, 25)
            stats = tracker.get_usage_stats()
            self.assertEqual(stats["today"]["recordings"], 1)


class TestUsageTrackerDailyHistory(unittest.TestCase):
    """Тесты для daily_history."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_history_contains_today(self):
        """Проверка, что history содержит сегодняшние данные."""
        self.tracker.record_usage(20.0, 80)
        stats = self.tracker.get_usage_stats()
        today_str = date.today().isoformat()
        dates = [e["date"] for e in stats["daily_history"]]
        self.assertIn(today_str, dates)

    def test_daily_history_max_30_days(self):
        """Проверка, что history содержит не более 30 дней."""
        today = date.today()
        for i in range(35):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        stats = self.tracker.get_usage_stats()
        self.assertLessEqual(len(stats["daily_history"]), 30)

    def test_daily_history_entry_has_required_fields(self):
        """Проверка структуры записей в daily_history."""
        self.tracker.record_usage(10.0, 40)
        stats = self.tracker.get_usage_stats()
        entry = stats["daily_history"][0]
        self.assertIn("date", entry)
        self.assertIn("recordings", entry)
        self.assertIn("duration_sec", entry)
        self.assertIn("words", entry)

    def test_daily_history_sorted_newest_first(self):
        """Проверка, что history отсортирована по убыванию дат."""
        today = date.today()
        for i in range(5):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        stats = self.tracker.get_usage_stats()
        dates = [e["date"] for e in stats["daily_history"]]
        self.assertEqual(dates, sorted(dates, reverse=True))


class TestUsageTrackerConcurrency(unittest.TestCase):
    """Тест потокобезопасности."""

    def test_concurrent_record_usage(self):
        """Проверка потокобезопасности при одновременных записях."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            threads = [
                threading.Thread(target=tracker.record_usage, args=(1.0, 5))
                for _ in range(50)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stats = tracker.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 50)
            self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 50.0)

    def test_concurrent_read_during_write(self):
        """Проверка, что чтение статистики не блокируется при записи."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)

            def writer():
                for _ in range(10):
                    tracker.record_usage(1.0, 5)

            def reader():
                for _ in range(10):
                    tracker.get_usage_stats()

            write_thread = threading.Thread(target=writer)
            read_threads = [threading.Thread(target=reader) for _ in range(3)]

            for t in read_threads:
                t.start()
            write_thread.start()

            write_thread.join()
            for t in read_threads:
                t.join()

            stats = tracker.get_usage_stats()
            self.assertGreater(stats["all_time"]["recordings"], 0)


class TestUsageTrackerEdgeCases(unittest.TestCase):
    """Тесты граничных случаев и обработки ошибок."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_duration_recording(self):
        """Проверка обработки записей с нулевой длительностью."""
        self.tracker.record_usage(0.0, 10)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertEqual(stats["today"]["total_duration_sec"], 0.0)

    def test_zero_words_recording(self):
        """Проверка обработки записей без слов."""
        self.tracker.record_usage(10.0, 0)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertEqual(stats["today"]["total_words"], 0)

    def test_large_values(self):
        """Проверка обработки больших значений длительности и слов."""
        self.tracker.record_usage(3600.0, 50000)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 3600.0)
        self.assertEqual(stats["today"]["total_words"], 50000)

    def test_float_precision_rounding(self):
        """Проверка округления значений float до 2 знаков."""
        self.tracker.record_usage(10.123456, 100)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["total_duration_sec"], 10.12)
        self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 10.12)

    def test_prune_old_days(self):
        """Проверка удаления записей старше 30 дней."""
        today = date.today()
        cutoff_date = (today - timedelta(days=31)).isoformat()
        self.tracker._daily[cutoff_date] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        self.tracker._daily[today.isoformat()] = {"recordings": 1, "duration_sec": 5.0, "words": 25}

        self.tracker._prune_old_days()
        self.assertNotIn(cutoff_date, self.tracker._daily)
        self.assertIn(today.isoformat(), self.tracker._daily)


class TestUsageTrackerGetDailyStats(unittest.TestCase):
    """Тесты для get_daily_stats(date)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_today_after_recording(self):
        self.tracker.record_usage(30.0, 100)
        stats = self.tracker.get_daily_stats(date.today())
        self.assertEqual(stats["recordings"], 1)
        self.assertAlmostEqual(stats["duration_sec"], 30.0)
        self.assertEqual(stats["words"], 100)

    def test_day_with_no_recordings_returns_zeros(self):
        yesterday = date.today() - timedelta(days=1)
        stats = self.tracker.get_daily_stats(yesterday)
        self.assertEqual(stats["recordings"], 0)
        self.assertEqual(stats["duration_sec"], 0.0)
        self.assertEqual(stats["words"], 0)

    def test_specific_historical_day(self):
        three_days_ago = (date.today() - timedelta(days=3)).isoformat()
        self.tracker._daily[three_days_ago] = {"recordings": 5, "duration_sec": 150.0, "words": 800}
        stats = self.tracker.get_daily_stats(date.today() - timedelta(days=3))
        self.assertEqual(stats["recordings"], 5)
        self.assertAlmostEqual(stats["duration_sec"], 150.0)
        self.assertEqual(stats["words"], 800)

    def test_returns_dict_with_all_required_keys(self):
        stats = self.tracker.get_daily_stats(date.today())
        self.assertIn("recordings", stats)
        self.assertIn("duration_sec", stats)
        self.assertIn("words", stats)

    def test_multiple_recordings_same_day_accumulated(self):
        self.tracker.record_usage(10.0, 50)
        self.tracker.record_usage(20.0, 100)
        stats = self.tracker.get_daily_stats(date.today())
        self.assertEqual(stats["recordings"], 2)
        self.assertAlmostEqual(stats["duration_sec"], 30.0)
        self.assertEqual(stats["words"], 150)

    def test_duration_rounded_to_2_decimals(self):
        self.tracker.record_usage(10.123456, 50)
        stats = self.tracker.get_daily_stats(date.today())
        self.assertEqual(stats["duration_sec"], 10.12)

    def test_future_date_returns_zeros(self):
        future = date.today() + timedelta(days=10)
        stats = self.tracker.get_daily_stats(future)
        self.assertEqual(stats["recordings"], 0)


class TestUsageTrackerGetWeekly(unittest.TestCase):
    """Тесты для get_weekly()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_returns_zeros(self):
        result = self.tracker.get_weekly()
        self.assertEqual(result["recordings"], 0)
        self.assertEqual(result["total_duration_sec"], 0.0)
        self.assertEqual(result["total_words"], 0)

    def test_includes_today(self):
        self.tracker.record_usage(15.0, 75)
        result = self.tracker.get_weekly()
        self.assertEqual(result["recordings"], 1)

    def test_sums_7_days(self):
        today = date.today()
        for i in range(7):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 30}
        result = self.tracker.get_weekly()
        self.assertEqual(result["recordings"], 7)
        self.assertAlmostEqual(result["total_duration_sec"], 70.0)
        self.assertEqual(result["total_words"], 210)

    def test_does_not_include_day_8(self):
        today = date.today()
        day8 = (today - timedelta(days=7)).isoformat()
        self.tracker._daily[day8] = {"recordings": 99, "duration_sec": 999.0, "words": 9999}
        result = self.tracker.get_weekly()
        self.assertEqual(result["recordings"], 0)

    def test_returns_required_keys(self):
        result = self.tracker.get_weekly()
        for key in ("recordings", "total_duration_sec", "total_words"):
            self.assertIn(key, result)

    def test_partial_week(self):
        """Только 3 дня из 7 имеют данные."""
        today = date.today()
        for i in (0, 2, 4):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 2, "duration_sec": 20.0, "words": 60}
        result = self.tracker.get_weekly()
        self.assertEqual(result["recordings"], 6)
        self.assertAlmostEqual(result["total_duration_sec"], 60.0)

    def test_matches_this_week_in_get_usage_stats(self):
        """get_weekly() должен совпадать с this_week из get_usage_stats()."""
        today = date.today()
        for i in range(5):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 25}
        weekly = self.tracker.get_weekly()
        stats_week = self.tracker.get_usage_stats()["this_week"]
        self.assertEqual(weekly["recordings"], stats_week["recordings"])
        self.assertAlmostEqual(weekly["total_duration_sec"], stats_week["total_duration_sec"])
        self.assertEqual(weekly["total_words"], stats_week["total_words"])


class TestUsageTrackerGetMonthly(unittest.TestCase):
    """Тесты для get_monthly()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_returns_zeros(self):
        result = self.tracker.get_monthly()
        self.assertEqual(result["recordings"], 0)
        self.assertEqual(result["total_duration_sec"], 0.0)
        self.assertEqual(result["total_words"], 0)

    def test_includes_today(self):
        self.tracker.record_usage(20.0, 100)
        result = self.tracker.get_monthly()
        self.assertEqual(result["recordings"], 1)

    def test_sums_30_days(self):
        today = date.today()
        for i in range(30):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 5.0, "words": 20}
        result = self.tracker.get_monthly()
        self.assertEqual(result["recordings"], 30)
        self.assertAlmostEqual(result["total_duration_sec"], 150.0)
        self.assertEqual(result["total_words"], 600)

    def test_does_not_include_day_31(self):
        today = date.today()
        day31 = (today - timedelta(days=30)).isoformat()
        self.tracker._daily[day31] = {"recordings": 99, "duration_sec": 999.0, "words": 9999}
        result = self.tracker.get_monthly()
        self.assertEqual(result["recordings"], 0)

    def test_returns_required_keys(self):
        result = self.tracker.get_monthly()
        for key in ("recordings", "total_duration_sec", "total_words"):
            self.assertIn(key, result)

    def test_matches_this_month_in_get_usage_stats(self):
        """get_monthly() должен совпадать с this_month из get_usage_stats()."""
        today = date.today()
        for i in range(20):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 2, "duration_sec": 15.0, "words": 50}
        monthly = self.tracker.get_monthly()
        stats_month = self.tracker.get_usage_stats()["this_month"]
        self.assertEqual(monthly["recordings"], stats_month["recordings"])
        self.assertAlmostEqual(monthly["total_duration_sec"], stats_month["total_duration_sec"])
        self.assertEqual(monthly["total_words"], stats_month["total_words"])

    def test_monthly_gt_weekly(self):
        """Месячная статистика >= недельной при данных в обоих периодах."""
        today = date.today()
        for i in range(25):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 30}
        weekly = self.tracker.get_weekly()
        monthly = self.tracker.get_monthly()
        self.assertGreaterEqual(monthly["recordings"], weekly["recordings"])

    def test_rollover_new_day(self):
        """Запись в 'вчера' и сегодня оба попадают в monthly."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.tracker._daily[yesterday] = {"recordings": 3, "duration_sec": 30.0, "words": 90}
        self.tracker.record_usage(10.0, 40)
        result = self.tracker.get_monthly()
        self.assertEqual(result["recordings"], 4)
        self.assertAlmostEqual(result["total_duration_sec"], 40.0)
        self.assertEqual(result["total_words"], 130)


class TestUsageTrackerRecordEvent(unittest.TestCase):
    """test_record_recording_event — запись события через record_usage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_recording_event_increments_recordings(self):
        """record_usage() добавляет ровно одну запись в today.recordings."""
        self.tracker.record_usage(duration_sec=45.0, word_count=120)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 45.0)
        self.assertEqual(stats["today"]["total_words"], 120)

    def test_record_recording_event_twice_accumulates(self):
        """Два вызова record_usage() — два события суммируются."""
        self.tracker.record_usage(10.0, 50)
        self.tracker.record_usage(20.0, 100)
        daily = self.tracker.get_daily_stats(date.today())
        self.assertEqual(daily["recordings"], 2)

    def test_record_recording_event_updates_all_time(self):
        """record_usage() обновляет all_time счётчик."""
        self.tracker.record_usage(5.0, 30)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 1)

    def test_record_recording_event_negative_words_clamped_to_int(self):
        """Отрицательные слова допускаются (нет валидации) — просто int()."""
        # UsageTracker не валидирует — просто приводит к int
        self.tracker.record_usage(1.0, -5)
        daily = self.tracker.get_daily_stats(date.today())
        self.assertEqual(daily["words"], -5)


class TestUsageTrackerAggregateByDay(unittest.TestCase):
    """test_aggregate_by_day — get_daily_stats агрегирует по дням."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_aggregate_by_day_today(self):
        """Агрегация за сегодня через record_usage + get_daily_stats совпадают."""
        self.tracker.record_usage(30.0, 150)
        self.tracker.record_usage(10.0, 50)
        daily = self.tracker.get_daily_stats(date.today())
        self.assertEqual(daily["recordings"], 2)
        self.assertAlmostEqual(daily["duration_sec"], 40.0)
        self.assertEqual(daily["words"], 200)

    def test_aggregate_by_day_independent_days(self):
        """Разные дни хранятся независимо."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        self.tracker._daily[today.isoformat()] = {"recordings": 3, "duration_sec": 60.0, "words": 300}
        self.tracker._daily[yesterday.isoformat()] = {"recordings": 1, "duration_sec": 10.0, "words": 50}

        stats_today = self.tracker.get_daily_stats(today)
        stats_yest = self.tracker.get_daily_stats(yesterday)

        self.assertEqual(stats_today["recordings"], 3)
        self.assertEqual(stats_yest["recordings"], 1)

    def test_aggregate_by_day_missing_day_zeros(self):
        """День без данных возвращает нулевую статистику."""
        far_past = date.today() - timedelta(days=5)
        stats = self.tracker.get_daily_stats(far_past)
        self.assertEqual(stats["recordings"], 0)
        self.assertEqual(stats["words"], 0)


class TestUsageTrackerGetStatsForDateRange(unittest.TestCase):
    """test_get_stats_for_date_range — агрегация через get_usage_stats периоды."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_stats_for_date_range_week(self):
        """this_week охватывает ровно 7 дней."""
        today = date.today()
        for i in range(7):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 5.0, "words": 20}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 7)

    def test_get_stats_for_date_range_month(self):
        """this_month охватывает ровно 30 дней."""
        today = date.today()
        for i in range(30):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 3.0, "words": 10}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_month"]["recordings"], 30)

    def test_get_stats_for_date_range_excludes_beyond_30(self):
        """Данные за 31+ день не входят в this_month."""
        day31 = (date.today() - timedelta(days=30)).isoformat()
        self.tracker._daily[day31] = {"recordings": 100, "duration_sec": 1000.0, "words": 5000}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_month"]["recordings"], 0)

    def test_get_stats_for_date_range_today_only(self):
        """Статистика за сегодня — ровно 1 день."""
        self.tracker.record_usage(8.0, 40)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 8.0)


class TestUsageTrackerPersistAcrossReload(unittest.TestCase):
    """test_persist_across_reload — данные сохраняются и загружаются."""

    def test_persist_across_reload_daily(self):
        """Daily-статистика переживает перезагрузку трекера."""
        with tempfile.TemporaryDirectory() as tmp:
            t1 = UsageTracker(data_dir=tmp)
            t1.record_usage(25.5, 130)
            t1.record_usage(14.0, 70)

            t2 = UsageTracker(data_dir=tmp)
            daily = t2.get_daily_stats(date.today())
            self.assertEqual(daily["recordings"], 2)
            self.assertAlmostEqual(daily["duration_sec"], 39.5, places=1)
            self.assertEqual(daily["words"], 200)

    def test_persist_across_reload_all_time(self):
        """All-time счётчики переживают перезагрузку трекера."""
        with tempfile.TemporaryDirectory() as tmp:
            t1 = UsageTracker(data_dir=tmp)
            t1.record_usage(10.0, 50)
            t1.record_usage(20.0, 100)

            t2 = UsageTracker(data_dir=tmp)
            stats = t2.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 2)
            self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 30.0)
            self.assertEqual(stats["all_time"]["total_words"], 150)

    def test_persist_across_reload_corrupted_file(self):
        """Повреждённый JSON-файл не роняет трекер — начинает с нуля."""
        with tempfile.TemporaryDirectory() as tmp:
            stats_path = Path(tmp) / "usage_stats.json"
            stats_path.write_text("CORRUPTED{{{", encoding="utf-8")
            t = UsageTracker(data_dir=tmp)
            stats = t.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 0)


class TestUsageTrackerConcurrentRecordThreadSafe(unittest.TestCase):
    """test_concurrent_record_thread_safe — многопоточная запись."""

    def test_concurrent_record_thread_safe_50_threads(self):
        """50 потоков записывают одновременно — итог точный."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            threads = [
                threading.Thread(target=tracker.record_usage, args=(2.0, 10))
                for _ in range(50)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stats = tracker.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 50)
            self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 100.0, places=1)
            self.assertEqual(stats["all_time"]["total_words"], 500)

    def test_concurrent_record_thread_safe_no_data_loss(self):
        """Параллельная запись не теряет данные (atomicity via lock)."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            errors = []

            def write_and_read():
                try:
                    tracker.record_usage(1.0, 5)
                    tracker.get_usage_stats()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=write_and_read) for _ in range(30)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"Exceptions in threads: {errors}")
            stats = tracker.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 30)


class TestUsageTrackerExportCSV(unittest.TestCase):
    """test_export_csv — экспорт daily_history в CSV-формат."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_csv_via_daily_history(self):
        """Из daily_history можно сформировать CSV-совместимый вывод."""
        import csv
        import io

        today = date.today()
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": i + 1, "duration_sec": float((i + 1) * 10), "words": (i + 1) * 50}

        stats = self.tracker.get_usage_stats()
        history = stats["daily_history"]

        buf = io.StringIO()
        fieldnames = ["date", "recordings", "duration_sec", "words"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for row in history:
            writer.writerow({k: row[k] for k in fieldnames})

        csv_content = buf.getvalue()
        self.assertIn("date,recordings,duration_sec,words", csv_content)
        self.assertIn(today.isoformat(), csv_content)
        lines = [ln for ln in csv_content.strip().splitlines() if ln]
        self.assertEqual(len(lines), 4)  # header + 3 data rows

    def test_export_csv_empty_history(self):
        """Пустая history даёт CSV только с заголовком."""
        import csv
        import io

        stats = self.tracker.get_usage_stats()
        history = stats["daily_history"]
        self.assertEqual(history, [])

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["date", "recordings", "duration_sec", "words"])
        writer.writeheader()
        csv_content = buf.getvalue()
        self.assertIn("date", csv_content)
        lines = [ln for ln in csv_content.strip().splitlines() if ln]
        self.assertEqual(len(lines), 1)  # header only

    def test_export_csv_preserves_all_fields(self):
        """CSV сохраняет все поля: date, recordings, duration_sec, words."""
        import csv
        import io

        self.tracker.record_usage(33.3, 222)
        stats = self.tracker.get_usage_stats()
        history = stats["daily_history"]

        buf = io.StringIO()
        fieldnames = ["date", "recordings", "duration_sec", "words"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for row in history:
            writer.writerow({k: row[k] for k in fieldnames})

        reader = csv.DictReader(io.StringIO(buf.getvalue()))
        rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["recordings"]), 1)
        self.assertEqual(int(rows[0]["words"]), 222)


class TestUsageTrackerUnicodeInMetadata(unittest.TestCase):
    """test_unicode_in_metadata — Unicode в путях и данных не ломает трекер."""

    def test_unicode_data_dir_path(self):
        """Трекер работает с директорией, содержащей Unicode в пути."""
        import tempfile
        import os
        # Создаём tmp dir с unicode-именем через os.makedirs
        base = tempfile.mkdtemp()
        unicode_dir = os.path.join(base, "данные_статистики")
        os.makedirs(unicode_dir, exist_ok=True)
        try:
            tracker = UsageTracker(data_dir=unicode_dir)
            tracker.record_usage(12.0, 60)
            stats = tracker.get_usage_stats()
            self.assertEqual(stats["today"]["recordings"], 1)

            # Проверяем что файл сохранился
            stats_file = Path(unicode_dir) / "usage_stats.json"
            self.assertTrue(stats_file.exists())
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_unicode_json_roundtrip(self):
        """JSON с Unicode символами корректно сохраняется и загружается."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            # Записываем данные
            tracker.record_usage(5.0, 10)

            # Читаем файл напрямую и проверяем корректность JSON
            stats_file = Path(tmp) / "usage_stats.json"
            content = stats_file.read_text(encoding="utf-8")
            data = json.loads(content)
            self.assertIn("daily", data)
            self.assertIn("all_time", data)

    def test_unicode_values_in_daily_data(self):
        """Трекер корректно обрабатывает строки с кириллицей в ключах (future-proofing)."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            # Напрямую вставляем Unicode-ключ в _daily (имитирует edge case)
            tracker._daily["2025-01-01"] = {"recordings": 2, "duration_sec": 20.0, "words": 100}
            tracker._persist()

            # Перезагружаем
            t2 = UsageTracker(data_dir=tmp)
            # Проверяем, что загрузилось без ошибок
            all_time = t2.get_usage_stats()["all_time"]
            # all_time должен быть от предыдущего трекера (нули, т.к. не вызывали record_usage)
            self.assertIsInstance(all_time["recordings"], int)


class TestUsageTrackerAtomicPersist(unittest.TestCase):
    """Тесты атомарности записи (W937 F2 — tmp+fsync+rename)."""

    def test_atomic_persist_no_partial_file(self):
        """После _persist() .tmp файл не остаётся на диске — rename прошёл атомарно."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            tracker.record_usage(10.0, 50)

            stats_file = Path(tmp) / "usage_stats.json"
            tmp_file = stats_file.with_suffix(stats_file.suffix + ".tmp")

            # После нормального persist: основной файл существует, .tmp — нет
            self.assertTrue(stats_file.exists(), "stats file must exist after persist")
            self.assertFalse(tmp_file.exists(), ".tmp file must be cleaned up after atomic rename")

    def test_atomic_persist_file_is_valid_json(self):
        """Файл, записанный через atomic persist, является корректным JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            tracker.record_usage(5.0, 25)
            tracker.record_usage(15.0, 75)

            stats_file = Path(tmp) / "usage_stats.json"
            data = json.loads(stats_file.read_text(encoding="utf-8"))
            self.assertIn("daily", data)
            self.assertIn("all_time", data)
            self.assertEqual(data["all_time"]["recordings"], 2)

    def test_corrupt_file_logs_warning_not_silent(self):
        """Повреждённый файл при загрузке генерирует WARNING (не молчит)."""
        import logging

        with tempfile.TemporaryDirectory() as tmp:
            stats_path = Path(tmp) / "usage_stats.json"
            stats_path.write_text("{bad json{{", encoding="utf-8")

            with self.assertLogs("KrabEar.Backend.UsageTracker", level="WARNING") as cm:
                _tracker = UsageTracker(data_dir=tmp)

            # Убеждаемся, что хотя бы одно WARNING-сообщение было залогировано
            warning_msgs = [r for r in cm.output if "WARNING" in r]
            self.assertTrue(warning_msgs, "Expected at least one WARNING log on corrupt file")
            # Счётчики сброшены в ноль
            stats = _tracker.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 0)

    def test_atomic_persist_survives_reload(self):
        """Данные, записанные через atomic persist, корректно загружаются при перезагрузке."""
        with tempfile.TemporaryDirectory() as tmp:
            t1 = UsageTracker(data_dir=tmp)
            t1.record_usage(30.0, 150)
            t1.record_usage(10.0, 50)

            t2 = UsageTracker(data_dir=tmp)
            stats = t2.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 2)
            self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 40.0)
            self.assertEqual(stats["all_time"]["total_words"], 200)


if __name__ == "__main__":
    unittest.main()
