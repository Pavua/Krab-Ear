"""Wave 203 — дополнительные тесты для IPCThrottle.

Покрывают сценарии, не вошедшие в test_ipc_throttle.py:
  - HEAVY-методы блокируются агрессивно (limit=5/min)
  - MEDIUM-методы блокируются умеренно (limit=30/min)
  - Незарегистрированный метод обрабатывается как light, не throttled в пределах лимита
  - Математика пополнения token bucket (rate = capacity/60)
  - Burst в пределах capacity — все разрешены
  - Burst сверх capacity — ровно capacity разрешено
  - Параллельные запросы сериализуются корректно: allowed <= capacity
  - reset_stats очищает бакеты статистики
  - Unicode-имена методов не вызывают исключений
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ipc_throttle import (
    IPCThrottle,
    _TokenBucket,
    _classify_method,
    HEAVY_METHODS,
    MEDIUM_METHODS,
    EXCLUDED_METHODS,
    _LIMITS,
)


class TestHeavyMethodsThrottleAggressively(unittest.TestCase):
    """HEAVY-методы имеют наименьший лимит — 5 вызовов/мин по умолчанию."""

    def test_default_heavy_limit_is_five(self):
        self.assertEqual(_LIMITS["heavy"], 5)

    def test_heavy_methods_blocked_after_five(self):
        throttle = IPCThrottle()
        method = "transcribe_paths"
        results = [throttle.check_rate(method) for _ in range(7)]
        allowed = sum(results)
        blocked = results.count(False)
        self.assertEqual(allowed, 5, "Exactly 5 heavy calls should be allowed")
        self.assertEqual(blocked, 2, "2 calls should be blocked")

    def test_all_heavy_methods_share_category_but_independent_buckets(self):
        """Каждый heavy-метод имеет независимый бакет; исчерпание одного не влияет на другой."""
        throttle = IPCThrottle()
        # Исчерпать transcribe_paths
        for _ in range(5):
            throttle.check_rate("transcribe_paths")
        self.assertFalse(throttle.check_rate("transcribe_paths"))
        # export_history имеет отдельный бакет — первый вызов разрешён
        self.assertTrue(throttle.check_rate("export_history"))


class TestMediumMethodsThrottleModerately(unittest.TestCase):
    """MEDIUM-методы имеют лимит 30/мин — агрессивнее light, мягче heavy."""

    def test_default_medium_limit_is_thirty(self):
        self.assertEqual(_LIMITS["medium"], 30)

    def test_medium_allows_exactly_30(self):
        throttle = IPCThrottle()
        method = "search_history"
        results = [throttle.check_rate(method) for _ in range(32)]
        self.assertEqual(sum(results), 30)

    def test_medium_limit_between_heavy_and_light(self):
        self.assertGreater(_LIMITS["medium"], _LIMITS["heavy"])
        self.assertLess(_LIMITS["medium"], _LIMITS["light"])


class TestUnregisteredMethodNotThrottled(unittest.TestCase):
    """Незарегистрированные методы классифицируются как light и не throttled в пределах лимита."""

    def test_unregistered_not_in_heavy_or_medium(self):
        method = "totally_new_nonexistent_method_xyz"
        self.assertNotIn(method, HEAVY_METHODS)
        self.assertNotIn(method, MEDIUM_METHODS)
        self.assertNotIn(method, EXCLUDED_METHODS)

    def test_unregistered_classified_as_light(self):
        self.assertEqual(_classify_method("brand_new_method_99"), "light")

    def test_unregistered_allowed_within_light_limit(self):
        throttle = IPCThrottle()  # light=120
        method = "non_existent_method_abc"
        results = [throttle.check_rate(method) for _ in range(120)]
        self.assertTrue(all(results), "All 120 calls within light limit should be allowed")

    def test_unregistered_blocked_after_light_limit(self):
        throttle = IPCThrottle()
        method = "non_existent_method_abc"
        for _ in range(120):
            throttle.check_rate(method)
        self.assertFalse(throttle.check_rate(method), "121st call should be blocked")


class TestTokenBucketRefillRate(unittest.TestCase):
    """Математика пополнения: rate = capacity / 60 токенов/сек."""

    def test_refill_rate_equals_capacity_over_60(self):
        bucket = _TokenBucket(60)
        self.assertAlmostEqual(bucket.rate, 1.0, places=6)

    def test_refill_rate_for_heavy_capacity(self):
        bucket = _TokenBucket(5)
        expected_rate = 5 / 60.0
        self.assertAlmostEqual(bucket.rate, expected_rate, places=9)

    def test_partial_refill_adds_fractional_tokens(self):
        """После 0.5 s при capacity=60 (rate=1t/s) должно добавиться ~0.5 токена."""
        bucket = _TokenBucket(60)
        # Опустошить бакет
        for _ in range(60):
            bucket.consume()
        self.assertLess(bucket._tokens, 1.0,
                        "After draining 60 tokens the bucket should hold < 1 token")

        # Подождать ~0.5 s и проверить, что токены пополнились частично
        time.sleep(0.5)
        bucket._refill()
        self.assertGreater(bucket._tokens, 0.3)   # минимум 0.3 токена добавилось
        self.assertLess(bucket._tokens, 1.0)       # но ещё < 1 целого токена

    def test_tokens_capped_at_capacity(self):
        """После долгого ожидания токены не превышают capacity."""
        bucket = _TokenBucket(5)
        # Опустошить
        for _ in range(5):
            bucket.consume()
        time.sleep(2.5)  # 5 * (1/60 sec-per-token)? — нет, rate=5/60=0.0833 t/s; 2.5s = ~0.2 токена
        # Вместо этого просто проверяем cap
        bucket._last_refill -= 1000.0  # симулируем давнее время
        bucket._refill()
        self.assertEqual(bucket._tokens, bucket.capacity)


class TestBurstWithinCapacityAllowed(unittest.TestCase):
    """Burst в пределах capacity — все вызовы должны пройти."""

    def test_burst_at_capacity_all_allowed(self):
        throttle = IPCThrottle(limits={"heavy": 10, "medium": 30, "light": 120})
        results = [throttle.check_rate("transcribe_paths") for _ in range(10)]
        self.assertTrue(all(results), f"All burst calls within capacity must be allowed, got {results}")

    def test_burst_light_at_capacity_all_allowed(self):
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 50})
        results = [throttle.check_rate("some_light_method") for _ in range(50)]
        self.assertTrue(all(results))


class TestBurstExceedsCapacityBlocked(unittest.TestCase):
    """Burst сверх capacity — ровно capacity разрешено, остальные заблокированы."""

    def test_burst_over_capacity_exact_count(self):
        cap = 7
        throttle = IPCThrottle(limits={"heavy": cap, "medium": 30, "light": 120})
        results = [throttle.check_rate("transcribe_paths") for _ in range(cap + 5)]
        self.assertEqual(sum(results), cap)
        self.assertEqual(results.count(False), 5)

    def test_burst_over_medium_capacity(self):
        cap = 10
        throttle = IPCThrottle(limits={"heavy": 5, "medium": cap, "light": 120})
        results = [throttle.check_rate("search_history") for _ in range(cap + 3)]
        self.assertEqual(sum(results), cap)


class TestConcurrentRequestsSerializeCorrectly(unittest.TestCase):
    """Параллельные запросы: allowed + throttled == total; allowed <= capacity."""

    def test_concurrent_heavy_total_consistency(self):
        cap = 8
        throttle = IPCThrottle(limits={"heavy": cap, "medium": 30, "light": 120})
        allowed_count = []
        lock = threading.Lock()

        def worker():
            for _ in range(5):
                result = throttle.check_rate("transcribe_paths")
                with lock:
                    allowed_count.append(result)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = len(allowed_count)
        allowed = sum(allowed_count)
        self.assertEqual(total, 50, "50 total attempts (10 threads x 5)")
        self.assertLessEqual(allowed, cap, f"Allowed ({allowed}) must not exceed capacity ({cap})")

    def test_concurrent_stats_consistent(self):
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 120})

        def burst():
            for _ in range(10):
                throttle.check_rate("transcribe_paths")

        threads = [threading.Thread(target=burst) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = throttle.get_throttle_stats()
        total_calls = stats["total_calls"]
        total_throttled = stats["total_throttled"]
        method_calls = stats["methods"]["transcribe_paths"]["calls"]

        self.assertEqual(total_calls, 50)
        self.assertEqual(method_calls, 50)
        # allowed = total - throttled; allowed <= 5
        self.assertLessEqual(total_calls - total_throttled, 5)


class TestResetClearsBuckets(unittest.TestCase):
    """reset_stats очищает счётчики; бакеты при этом сохраняются (они per-method)."""

    def test_reset_stats_clears_call_counts(self):
        throttle = IPCThrottle()
        throttle.check_rate("get_clipboard_history")
        throttle.check_rate("get_clipboard_history")
        throttle.reset_stats()
        stats = throttle.get_throttle_stats()
        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["total_throttled"], 0)
        self.assertEqual(stats["methods"], {})

    def test_reset_stats_does_not_clear_buckets_refill(self):
        """После сброса статистики бакеты ещё существуют, новые вызовы продолжают работать."""
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 120})
        # Исчерпать heavy bucket
        for _ in range(5):
            throttle.check_rate("transcribe_paths")
        # Сброс статистики
        throttle.reset_stats()
        # Бакет НЕ сбрасывается — следующий вызов должен быть заблокирован
        self.assertFalse(throttle.check_rate("transcribe_paths"),
                         "Bucket should still be empty after reset_stats")

    def test_reset_stats_multiple_times_is_idempotent(self):
        throttle = IPCThrottle()
        throttle.check_rate("get_clipboard_history")
        throttle.reset_stats()
        throttle.reset_stats()
        stats = throttle.get_throttle_stats()
        self.assertEqual(stats["total_calls"], 0)


class TestUnicodeMethodName(unittest.TestCase):
    """Unicode-имена методов не должны вызывать исключений."""

    def test_unicode_method_name_classify(self):
        # Не должен бросить исключение
        category = _classify_method("метод_на_кириллице")
        self.assertEqual(category, "light")

    def test_unicode_method_check_rate_no_exception(self):
        throttle = IPCThrottle()
        # Не должен бросить исключение
        result = throttle.check_rate("кириллический_метод")
        self.assertIsInstance(result, bool)

    def test_unicode_method_in_stats(self):
        throttle = IPCThrottle()
        throttle.check_rate("español_método")
        stats = throttle.get_throttle_stats()
        self.assertIn("español_método", stats["methods"])

    def test_unicode_get_wait_time_no_exception(self):
        throttle = IPCThrottle()
        wait = throttle.get_wait_time("日本語メソッド")
        self.assertGreaterEqual(wait, 0.0)


if __name__ == "__main__":
    unittest.main()
