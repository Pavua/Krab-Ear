"""Unit tests для backend/llm_rewriter.py — CircuitBreaker + LLMRewriter."""

import unittest
from unittest.mock import patch, MagicMock


class CircuitBreakerTestCase(unittest.TestCase):
    """Тесты state machine: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    def setUp(self):
        from backend.llm_rewriter import CircuitBreaker
        self.breaker = CircuitBreaker(fail_threshold=3, initial_reset_sec=60, max_reset_sec=600)

    def test_initial_state_closed(self):
        self.assertEqual(self.breaker.state, "closed")

    def test_closed_allows_requests(self):
        self.assertTrue(self.breaker.allow_request())

    def test_one_failure_stays_closed(self):
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.allow_request())

    def test_two_failures_stays_closed(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "closed")

    def test_three_consecutive_failures_opens(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

    def test_success_resets_failure_counter(self):
        """fail, fail, success, fail, fail — circuit должен остаться CLOSED."""
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_success()
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "closed")

    def test_open_blocks_requests_immediately_after_open(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")
        self.assertFalse(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_open_transitions_to_half_open_after_cooldown(self, mock_monotonic):
        """После reset_sec allow_request() переходит в HALF_OPEN и возвращает True."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1059.0
        self.assertFalse(self.breaker.allow_request())
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1061.0
        self.assertTrue(self.breaker.allow_request())
        self.assertEqual(self.breaker.state, "half_open")

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_blocks_second_request(self, mock_monotonic):
        """В HALF_OPEN только первый request проходит, остальные False."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()
        mock_monotonic.return_value = 1061.0
        self.assertTrue(self.breaker.allow_request())
        self.assertFalse(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_success_transitions_to_closed(self, mock_monotonic):
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()
        mock_monotonic.return_value = 1061.0
        self.breaker.allow_request()
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_success_resets_backoff(self, mock_monotonic):
        """После HALF_OPEN → CLOSED → новое открытие должно иметь initial_reset_sec cooldown."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()

        mock_monotonic.return_value = 1061.0
        self.breaker.allow_request()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1061.0 + 119.0
        self.assertFalse(self.breaker.allow_request())
        mock_monotonic.return_value = 1061.0 + 121.0
        self.assertTrue(self.breaker.allow_request())
        self.breaker.record_success()

        mock_monotonic.return_value = 2000.0
        for _ in range(3):
            self.breaker.record_failure()

        mock_monotonic.return_value = 2000.0 + 59.0
        self.assertFalse(self.breaker.allow_request())
        mock_monotonic.return_value = 2000.0 + 61.0
        self.assertTrue(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_exponential_backoff_doubles_on_probe_failure(self, mock_monotonic):
        """HALF_OPEN fail удваивает cooldown (60 → 120)."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()

        mock_monotonic.return_value = 1061.0
        self.assertTrue(self.breaker.allow_request())
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1061.0 + 119.0
        self.assertFalse(self.breaker.allow_request())
        mock_monotonic.return_value = 1061.0 + 121.0
        self.assertTrue(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_backoff_caps_at_max_reset_sec(self, mock_monotonic):
        """После многих неудачных проб cooldown не превышает max_reset_sec."""
        breaker = __import__("backend.llm_rewriter", fromlist=["CircuitBreaker"]).CircuitBreaker(
            fail_threshold=1, initial_reset_sec=60, max_reset_sec=300
        )
        t = 1000.0
        mock_monotonic.return_value = t
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")

        for _ in range(10):
            t += 1000.0
            mock_monotonic.return_value = t
            self.assertTrue(breaker.allow_request())
            breaker.record_failure()

        t_open = t
        mock_monotonic.return_value = t_open + 299.0
        self.assertFalse(breaker.allow_request())
        mock_monotonic.return_value = t_open + 301.0
        self.assertTrue(breaker.allow_request())


if __name__ == "__main__":
    unittest.main()
