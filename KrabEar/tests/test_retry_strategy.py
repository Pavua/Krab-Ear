"""test_retry_strategy.py — Тесты для RetryStrategy / RetryConfig."""
import concurrent.futures
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.retry_strategy import RetryConfig, RetryStrategy  # noqa: E402


class TestRetryConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = RetryConfig()
        self.assertEqual(cfg.max_retries, 2)
        self.assertAlmostEqual(cfg.backoff_factor, 1.5)
        self.assertIn("timeout", cfg.retry_on)
        self.assertIn("model_error", cfg.retry_on)

    def test_custom_values(self):
        cfg = RetryConfig(max_retries=5, backoff_factor=2.0, retry_on=["timeout"])
        self.assertEqual(cfg.max_retries, 5)
        self.assertAlmostEqual(cfg.backoff_factor, 2.0)
        self.assertEqual(cfg.retry_on, ["timeout"])


class TestShouldRetry(unittest.TestCase):
    def setUp(self):
        self.strategy = RetryStrategy(RetryConfig(max_retries=2, retry_on=["timeout", "model_error"]))

    def test_retry_on_timeout_within_limit(self):
        err = concurrent.futures.TimeoutError()
        self.assertTrue(self.strategy.should_retry(err, attempt=0))
        self.assertTrue(self.strategy.should_retry(err, attempt=1))

    def test_no_retry_when_attempts_exhausted(self):
        err = concurrent.futures.TimeoutError()
        self.assertFalse(self.strategy.should_retry(err, attempt=2))
        self.assertFalse(self.strategy.should_retry(err, attempt=10))

    def test_no_retry_for_unknown_error(self):
        err = ValueError("bad input")
        self.assertFalse(self.strategy.should_retry(err, attempt=0))

    def test_retry_on_memory_error(self):
        err = MemoryError("OOM")
        self.assertTrue(self.strategy.should_retry(err, attempt=0))

    def test_retry_on_runtime_error(self):
        err = RuntimeError("model crash")
        self.assertTrue(self.strategy.should_retry(err, attempt=0))


class TestGetDelay(unittest.TestCase):
    def test_delay_grows_exponentially(self):
        strategy = RetryStrategy(RetryConfig(backoff_factor=2.0))
        self.assertAlmostEqual(strategy.get_delay(0), 1.0)   # 2^0
        self.assertAlmostEqual(strategy.get_delay(1), 2.0)   # 2^1
        self.assertAlmostEqual(strategy.get_delay(2), 4.0)   # 2^2

    def test_default_backoff_factor(self):
        strategy = RetryStrategy()
        self.assertAlmostEqual(strategy.get_delay(0), 1.0)
        self.assertAlmostEqual(strategy.get_delay(1), 1.5)


class TestExecuteWithRetry(unittest.TestCase):
    def setUp(self):
        self.cfg = RetryConfig(max_retries=2, backoff_factor=0.0, retry_on=["timeout", "model_error"])
        self.strategy = RetryStrategy(self.cfg)

    def test_success_on_first_attempt(self):
        fn = MagicMock(return_value="ok")
        result = self.strategy.execute_with_retry(fn, "audio")
        self.assertEqual(result, "ok")
        fn.assert_called_once_with("audio")

    def test_retry_then_success(self):
        fn = MagicMock(side_effect=[concurrent.futures.TimeoutError(), "ok"])
        with patch("time.sleep"):
            result = self.strategy.execute_with_retry(fn)
        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 2)

    def test_all_attempts_fail_raises(self):
        fn = MagicMock(side_effect=MemoryError("OOM"))
        with patch("time.sleep"):
            with self.assertRaises(MemoryError):
                self.strategy.execute_with_retry(fn)
        # initial attempt + max_retries retries
        self.assertEqual(fn.call_count, 3)

    def test_non_retryable_error_raises_immediately(self):
        fn = MagicMock(side_effect=ValueError("bad"))
        with self.assertRaises(ValueError):
            self.strategy.execute_with_retry(fn)
        fn.assert_called_once()

    def test_passes_args_and_kwargs(self):
        fn = MagicMock(return_value=42)
        result = self.strategy.execute_with_retry(fn, 1, 2, key="val")
        fn.assert_called_once_with(1, 2, key="val")
        self.assertEqual(result, 42)

    def test_sleep_called_with_correct_delay(self):
        cfg = RetryConfig(max_retries=2, backoff_factor=2.0, retry_on=["timeout"])
        strategy = RetryStrategy(cfg)
        fn = MagicMock(side_effect=[concurrent.futures.TimeoutError(), concurrent.futures.TimeoutError(), "ok"])
        with patch("time.sleep") as mock_sleep:
            result = strategy.execute_with_retry(fn)
        self.assertEqual(result, "ok")
        mock_sleep.assert_any_call(1.0)  # attempt 0: 2^0
        mock_sleep.assert_any_call(2.0)  # attempt 1: 2^1


class TestGetRetryStats(unittest.TestCase):
    def setUp(self):
        self.cfg = RetryConfig(max_retries=2, backoff_factor=0.0, retry_on=["timeout", "model_error"])

    def test_initial_stats_empty(self):
        strategy = RetryStrategy(self.cfg)
        stats = strategy.get_retry_stats()
        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["total_retries"], 0)
        self.assertEqual(stats["total_successes"], 0)
        self.assertAlmostEqual(stats["success_rate"], 0.0)
        self.assertAlmostEqual(stats["avg_retries_per_success"], 0.0)

    def test_stats_after_successful_call(self):
        strategy = RetryStrategy(self.cfg)
        fn = MagicMock(return_value="ok")
        strategy.execute_with_retry(fn)
        stats = strategy.get_retry_stats()
        self.assertEqual(stats["total_calls"], 1)
        self.assertEqual(stats["total_successes"], 1)
        self.assertAlmostEqual(stats["success_rate"], 1.0)
        self.assertEqual(stats["total_retries"], 0)

    def test_stats_after_retry_then_success(self):
        strategy = RetryStrategy(self.cfg)
        fn = MagicMock(side_effect=[concurrent.futures.TimeoutError(), "ok"])
        with patch("time.sleep"):
            strategy.execute_with_retry(fn)
        stats = strategy.get_retry_stats()
        self.assertEqual(stats["total_calls"], 1)
        self.assertEqual(stats["total_successes"], 1)
        self.assertEqual(stats["total_retries"], 1)
        self.assertAlmostEqual(stats["avg_retries_per_success"], 1.0)

    def test_stats_after_total_failure(self):
        strategy = RetryStrategy(self.cfg)
        fn = MagicMock(side_effect=MemoryError("OOM"))
        with patch("time.sleep"):
            with self.assertRaises(MemoryError):
                strategy.execute_with_retry(fn)
        stats = strategy.get_retry_stats()
        self.assertEqual(stats["total_calls"], 1)
        self.assertEqual(stats["total_successes"], 0)
        self.assertAlmostEqual(stats["success_rate"], 0.0)

    def test_reset_stats(self):
        strategy = RetryStrategy(self.cfg)
        fn = MagicMock(return_value="ok")
        strategy.execute_with_retry(fn)
        strategy.reset_stats()
        stats = strategy.get_retry_stats()
        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["total_successes"], 0)

    def test_success_rate_mixed(self):
        strategy = RetryStrategy(self.cfg)
        ok_fn = MagicMock(return_value="ok")
        fail_fn = MagicMock(side_effect=ValueError("bad"))  # non-retryable
        strategy.execute_with_retry(ok_fn)
        with self.assertRaises(ValueError):
            strategy.execute_with_retry(fail_fn)
        stats = strategy.get_retry_stats()
        self.assertEqual(stats["total_calls"], 2)
        self.assertEqual(stats["total_successes"], 1)
        self.assertAlmostEqual(stats["success_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
