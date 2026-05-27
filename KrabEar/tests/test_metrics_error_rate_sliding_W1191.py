"""Тесты W1191: sliding-window error_rate через deque (W1169 F2 MED).

Проверяет, что ошибки из раннего burst'а вытесняются чистыми запросами
и error_rate корректно снижается по мере заполнения окна.
"""

import sys
import os
import unittest
from collections import deque

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.metrics_collector import MetricsCollector


class TestErrorRateDropsAfterWindowOfCleanRequests(unittest.TestCase):
    """error_rate должен снижаться по мере накопления чистых запросов в окне.

    Знаменатель error_rate = len(latencies) + len(error_events), т.е. суммарный
    размер обоих bounded deque'ов. Оба ограничены window_size. Ошибки из раннего
    burst'а не вытесняются напрямую чистыми запросами — числитель стабилен, но
    знаменатель растёт, что снижает rate. Если новые ошибки вытесняют старые из
    error_events (maxlen заполнен) — rate ещё быстрее падает к нулю.
    """

    def test_error_rate_drops_after_window_of_clean_requests(self):
        """Burst ошибок в начале не должен навсегда загрязнять error_rate.

        Паттерн:
          - 5 ранних ошибок → error_rate = 1.0 (нет latencies, только errors)
          - 1000 чистых запросов (window=1000) → latencies=1000, error_events=5
            error_rate = 5/(5+1000) ≈ 0.005, а не "навечно 1.0"
        """
        window = 1000
        mc = MetricsCollector(window_size=window)

        # 5 ранних ошибок
        for _ in range(5):
            mc.record(0.0, 0.0, is_error=True)

        # Сразу после burst'а — все запросы ошибки (latencies пусто)
        summary = mc.get_summary()
        self.assertAlmostEqual(summary["error_rate"], 1.0, places=4,
                               msg="Сразу после burst — все запросы ошибки")

        # Теперь записываем полное окно чистых запросов
        for i in range(window):
            mc.record(float(i + 1) * 10.0, 0.9, is_error=False)

        summary = mc.get_summary()
        # error_events=5, latencies=1000 → rate=5/(5+1000)=0.00499...
        self.assertLess(summary["error_rate"], 0.01,
                        f"После {window} чистых запросов error_rate должен быть <0.01, "
                        f"получили {summary['error_rate']}")
        expected = round(5 / (5 + window), 4)
        self.assertAlmostEqual(summary["error_rate"], expected, places=3,
                               msg=f"error_rate=5/(5+{window})={expected}")

    def test_error_rate_reduces_proportionally_as_clean_fills_window(self):
        """error_rate снижается пропорционально росту latencies-окна."""
        window = 100
        mc = MetricsCollector(window_size=window)

        # 10 ошибок в начале
        for _ in range(10):
            mc.record(0.0, 0.0, is_error=True)

        # После 50 чистых: rate = 10/(10+50) ≈ 0.1667
        for i in range(50):
            mc.record(float(i + 1), 0.9)
        summary = mc.get_summary()
        expected_50 = round(10 / (10 + 50), 4)
        self.assertAlmostEqual(summary["error_rate"], expected_50, places=4,
                               msg=f"10 ошибок / (10+50) = {expected_50}")

        # После ещё 50 (итого 100 чистых, window полный): rate = 10/(10+100) ≈ 0.0909
        for i in range(50):
            mc.record(float(i + 51), 0.9)
        summary = mc.get_summary()
        expected_100 = round(10 / (10 + 100), 4)
        self.assertAlmostEqual(summary["error_rate"], expected_100, places=4,
                               msg=f"10 ошибок / (10+100) = {expected_100}")


class TestErrorRateUsesBoundedDeque(unittest.TestCase):
    """error_events — это deque с ограниченным maxlen, а не безграничный счётчик."""

    def test_error_rate_uses_bounded_deque(self):
        """error_events должен быть deque с maxlen == window_size."""
        mc = MetricsCollector(window_size=5)

        # Проверяем тип и ограничение
        self.assertIsInstance(mc.error_events, deque,
                              "error_events должен быть collections.deque")
        self.assertEqual(mc.error_events.maxlen, 5,
                         "error_events.maxlen должен совпадать с window_size")

    def test_error_events_bounded_overflow(self):
        """При превышении window_size старые ошибки вытесняются из error_events."""
        window = 3
        mc = MetricsCollector(window_size=window)

        # Записываем 6 ошибок — должны остаться только последние 3
        for _ in range(6):
            mc.record(0.0, 0.0, is_error=True)

        self.assertEqual(len(mc.error_events), window,
                         f"После 6 ошибок при window=3 в deque должно быть ровно 3")

    def test_error_events_contains_timestamps(self):
        """error_events хранит float-timestamps (time.monotonic), не int."""
        mc = MetricsCollector(window_size=10)
        mc.record(0.0, 0.0, is_error=True)

        self.assertEqual(len(mc.error_events), 1)
        ts = mc.error_events[0]
        self.assertIsInstance(ts, float,
                              "Элементы error_events должны быть float (timestamp)")
        self.assertGreater(ts, 0.0, "timestamp должен быть положительным")


class TestLegacyErrorsAliasStillWorks(unittest.TestCase):
    """`errors` property должна сохранять обратную совместимость."""

    def test_legacy_errors_alias_still_works(self):
        """mc.errors возвращает len(error_events) — не падает и не ломает тесты."""
        mc = MetricsCollector()

        # Изначально 0
        self.assertEqual(mc.errors, 0)

        # После записи ошибок отражает их количество
        mc.record(0.0, 0.0, is_error=True)
        mc.record(0.0, 0.0, is_error=True)
        self.assertEqual(mc.errors, 2)

        # После добавления чистых не меняется
        mc.record(100.0, 0.9)
        self.assertEqual(mc.errors, 2)

    def test_errors_is_property_not_int(self):
        """errors должен быть property, а не обычным int-атрибутом."""
        mc = MetricsCollector()
        # Если errors — property, то присваивание напрямую бросает AttributeError
        with self.assertRaises(AttributeError):
            mc.errors = 99  # type: ignore[misc]

    def test_errors_bounded_by_window(self):
        """mc.errors не превышает window_size даже при тысячах ошибок."""
        window = 5
        mc = MetricsCollector(window_size=window)

        for _ in range(100):
            mc.record(0.0, 0.0, is_error=True)

        self.assertLessEqual(mc.errors, window,
                             "mc.errors не должен превышать window_size")
        self.assertEqual(mc.errors, window)


if __name__ == "__main__":
    unittest.main()
