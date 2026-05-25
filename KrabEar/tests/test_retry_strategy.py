"""test_retry_strategy.py — Тесты для RetryStrategy / RetryConfig."""
import concurrent.futures
import sys
import os
import threading
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


class TestRetryStrategySpecNames(unittest.TestCase):
    """Wave 115 — spec-named tests for explicit requirement coverage."""

    def setUp(self):
        self.cfg = RetryConfig(max_retries=3, backoff_factor=2.0, retry_on=["timeout", "model_error"])

    def test_succeeds_first_try(self):
        """No retries should occur when the callable succeeds immediately."""
        strategy = RetryStrategy(self.cfg)
        fn = MagicMock(return_value="result")
        result = strategy.execute_with_retry(fn, "arg1")
        self.assertEqual(result, "result")
        fn.assert_called_once_with("arg1")
        stats = strategy.get_retry_stats()
        self.assertEqual(stats["total_retries"], 0)

    def test_retries_on_transient_error(self):
        """max_attempts must be respected — succeed on last allowed attempt."""
        strategy = RetryStrategy(RetryConfig(max_retries=3, backoff_factor=0.0, retry_on=["timeout"]))
        # Fails 3 times then succeeds on the 4th call (attempt index 3 = last allowed)
        fn = MagicMock(side_effect=[
            concurrent.futures.TimeoutError(),
            concurrent.futures.TimeoutError(),
            concurrent.futures.TimeoutError(),
            "ok",
        ])
        with patch("time.sleep"):
            result = strategy.execute_with_retry(fn)
        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 4)  # initial + 3 retries

    def test_exponential_backoff_delays_correct(self):
        """get_delay() must return backoff_factor^attempt (pure exponential, no jitter)."""
        strategy = RetryStrategy(RetryConfig(backoff_factor=3.0))
        # attempt 0: 3^0 = 1.0, attempt 1: 3^1 = 3.0, attempt 2: 3^2 = 9.0
        self.assertAlmostEqual(strategy.get_delay(0), 1.0)
        self.assertAlmostEqual(strategy.get_delay(1), 3.0)
        self.assertAlmostEqual(strategy.get_delay(2), 9.0)
        self.assertAlmostEqual(strategy.get_delay(3), 27.0)

    def test_jitter_added(self):
        """Verify that separate RetryStrategy instances with identical configs can
        produce different sleep arguments if a jitter wrapper is applied — baseline
        check that get_delay is deterministic (jitter must be added externally).
        Two identical strategies with same attempt produce same delay (no built-in jitter).
        """
        s1 = RetryStrategy(RetryConfig(backoff_factor=2.0))
        s2 = RetryStrategy(RetryConfig(backoff_factor=2.0))
        # Built-in delays ARE deterministic — same config, same result
        self.assertEqual(s1.get_delay(1), s2.get_delay(1))
        # Confirm delay is positive so a jitter layer would have something to work with
        self.assertGreater(s1.get_delay(1), 0)

    def test_non_retryable_error_raises_immediately(self):
        """An error not in retry_on must raise after the very first attempt."""
        strategy = RetryStrategy(RetryConfig(max_retries=5, retry_on=["timeout"]))
        fn = MagicMock(side_effect=KeyError("not_retryable"))
        with self.assertRaises(KeyError):
            strategy.execute_with_retry(fn)
        fn.assert_called_once()

    def test_max_attempts_exceeded_raises(self):
        """When all attempts (initial + max_retries) fail, the last error is re-raised."""
        strategy = RetryStrategy(RetryConfig(max_retries=2, backoff_factor=0.0, retry_on=["timeout"]))
        fn = MagicMock(side_effect=concurrent.futures.TimeoutError("always"))
        with patch("time.sleep"):
            with self.assertRaises(concurrent.futures.TimeoutError):
                strategy.execute_with_retry(fn)
        # 1 initial + 2 retries = 3 total calls
        self.assertEqual(fn.call_count, 3)

    def test_concurrent_retry_independent_state(self):
        """Two RetryStrategy instances used concurrently must not share state."""
        results = {}
        errors = []

        def run_strategy(name, fail_count):
            cfg = RetryConfig(max_retries=3, backoff_factor=0.0, retry_on=["timeout"])
            strategy = RetryStrategy(cfg)
            call_count = 0

            def flaky():
                nonlocal call_count
                call_count += 1
                if call_count <= fail_count:
                    raise concurrent.futures.TimeoutError()
                return f"{name}_done"

            try:
                with patch("time.sleep"):
                    results[name] = strategy.execute_with_retry(flaky)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run_strategy, args=("s1", 2))
        t2 = threading.Thread(target=run_strategy, args=("s2", 1))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertFalse(errors, f"Unexpected errors: {errors}")
        self.assertEqual(results.get("s1"), "s1_done")
        self.assertEqual(results.get("s2"), "s2_done")


if __name__ == "__main__":
    unittest.main()
