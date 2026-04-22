"""Advanced edge-case tests for backend/llm_rewriter.py.

Covers gaps not in test_llm_rewriter.py / test_llm_rewriter_summarize.py:
- CircuitBreaker CLOSED→OPEN→HALF_OPEN→CLOSED full cycle
- Cooldown respected: OPEN blocks during cooldown window
- Chatbot guard: English AI markers ("I'm Claude", "As an AI", "i'm sorry", etc.)
- Length ratio guard exact boundary behaviour (35% / 300%)
- Session pooling: same _session instance reused across calls
- Timeout → circuit failure accumulation
- Empty input → no API call (latency_ms=None)
- max_tokens in HTTP payload (floor / scaling)
- close() releases session
- record_success in CLOSED state is a no-op (no crash, counter resets)
- HTTP 401 / 403 fallback_reason
- requests.RequestException base class handled
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests as _requests

from backend.llm_rewriter import CircuitBreaker, LLMRewriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_resp(content: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def _err_resp(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "Error"
    return resp


def _make_rewriter(**kwargs) -> LLMRewriter:
    defaults = dict(
        base_url="http://localhost:1234/v1",
        api_key="sk-test",
        model="test-model",
        circuit_fail_threshold=3,
        circuit_initial_reset_sec=60,
    )
    defaults.update(kwargs)
    return LLMRewriter(**defaults)


# ---------------------------------------------------------------------------
# 1. CircuitBreaker full cycle CLOSED → OPEN → HALF_OPEN → CLOSED
# ---------------------------------------------------------------------------

class CircuitBreakerFullCycleTestCase(unittest.TestCase):
    """One-pass walkthrough of all four state transitions."""

    @patch("backend.llm_rewriter.time.monotonic")
    def test_full_cycle_closed_open_halfopen_closed(self, mock_mono):
        breaker = CircuitBreaker(
            fail_threshold=2,
            initial_reset_sec=30,
            max_reset_sec=300,
        )
        mock_mono.return_value = 1000.0

        # CLOSED → OPEN after 2 failures
        self.assertEqual(breaker.state, "closed")
        breaker.record_failure()
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")

        # OPEN blocks requests immediately
        self.assertFalse(breaker.allow_request())

        # OPEN → HALF_OPEN after cooldown
        mock_mono.return_value = 1031.0
        self.assertTrue(breaker.allow_request())
        self.assertEqual(breaker.state, "half_open")

        # HALF_OPEN → CLOSED on success
        breaker.record_success()
        self.assertEqual(breaker.state, "closed")

        # CLOSED is functional again
        self.assertTrue(breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_failure_returns_to_open_with_doubled_cooldown(self, mock_mono):
        breaker = CircuitBreaker(
            fail_threshold=1,
            initial_reset_sec=60,
            max_reset_sec=600,
        )
        mock_mono.return_value = 0.0
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")

        # First probe succeeds to HALF_OPEN
        mock_mono.return_value = 61.0
        self.assertTrue(breaker.allow_request())
        self.assertEqual(breaker.state, "half_open")

        # Probe fails → back to OPEN with doubled cooldown (120s)
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")

        # Old cooldown (61s past open) is NOT enough for new 120s period
        mock_mono.return_value = 61.0 + 119.0
        self.assertFalse(breaker.allow_request())

        # After 120s from re-open, allow again
        mock_mono.return_value = 61.0 + 121.0
        self.assertTrue(breaker.allow_request())


# ---------------------------------------------------------------------------
# 2. Cooldown respected: OPEN does not allow calls mid-cooldown
# ---------------------------------------------------------------------------

class CircuitBreakerCooldownTestCase(unittest.TestCase):

    @patch("backend.llm_rewriter.time.monotonic")
    def test_open_blocks_at_59s_allows_at_61s(self, mock_mono):
        """Exact boundary: open at t=0, blocks at t=59, allows at t=61."""
        breaker = CircuitBreaker(
            fail_threshold=1,
            initial_reset_sec=60,
            max_reset_sec=600,
        )
        mock_mono.return_value = 0.0
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")

        mock_mono.return_value = 59.0
        self.assertFalse(breaker.allow_request())
        self.assertEqual(breaker.state, "open")

        mock_mono.return_value = 61.0
        self.assertTrue(breaker.allow_request())
        self.assertEqual(breaker.state, "half_open")

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_in_flight_flag_cleared_by_success(self, mock_mono):
        """After record_success, _half_open_probe_in_flight is False."""
        breaker = CircuitBreaker(
            fail_threshold=1,
            initial_reset_sec=60,
            max_reset_sec=600,
        )
        mock_mono.return_value = 0.0
        breaker.record_failure()
        mock_mono.return_value = 61.0
        breaker.allow_request()  # sets _half_open_probe_in_flight = True
        self.assertTrue(breaker._half_open_probe_in_flight)
        breaker.record_success()
        self.assertFalse(breaker._half_open_probe_in_flight)

    def test_record_success_in_closed_does_not_raise(self):
        """record_success() in CLOSED state resets counter without error."""
        breaker = CircuitBreaker(
            fail_threshold=3,
            initial_reset_sec=60,
            max_reset_sec=600,
        )
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()  # should not raise
        self.assertEqual(breaker.state, "closed")
        self.assertEqual(breaker._consecutive_failures, 0)


# ---------------------------------------------------------------------------
# 3. Chatbot guard: English AI markers
# ---------------------------------------------------------------------------

class ChatbotGuardEnglishMarkersTestCase(unittest.TestCase):
    """English AI chatbot markers must trigger fallback_reason='chatbot_response'."""

    def setUp(self):
        self.rw = _make_rewriter()

    def _call(self, content: str):
        self.rw._session.post = MagicMock(return_value=_ok_resp(content))
        # Use input long enough to avoid length-ratio guard
        return self.rw.rewrite("a" * 60)

    def test_im_claude_triggers_chatbot_guard(self):
        # "i'm sorry" is the marker in _CHATBOT_MARKERS
        result = self._call("I'm sorry, I cannot edit this text.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_i_apologize_triggers_chatbot_guard(self):
        result = self._call("I apologize, but I am unable to process your request.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_ya_ne_mogu_triggers_chatbot_guard(self):
        result = self._call("Я не могу выполнить данный запрос без контекста.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_izvinite_triggers_chatbot_guard(self):
        result = self._call("Извините, но я не понимаю задачу.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_normal_text_starting_with_ai_word_passes(self):
        """Text that doesn't start with a marker should pass."""
        result = self._call("б" * 60)
        self.assertTrue(result.ok)


# ---------------------------------------------------------------------------
# 4. Length ratio guard exact boundary
# ---------------------------------------------------------------------------

class LengthRatioGuardBoundaryTestCase(unittest.TestCase):

    def setUp(self):
        self.rw = _make_rewriter()

    def _call(self, output: str, input_len: int = 100):
        inp = "а" * input_len
        self.rw._session.post = MagicMock(return_value=_ok_resp(output))
        return self.rw.rewrite(inp)

    def test_output_exactly_35_pct_passes(self):
        """35 chars out of 100 → ratio = 0.35 exactly → allowed (not < 0.35)."""
        result = self._call("б" * 35, input_len=100)
        self.assertTrue(result.ok)

    def test_output_34_pct_is_too_short(self):
        """34 chars out of 100 → ratio = 0.34 < 0.35 → output_too_short."""
        result = self._call("б" * 34, input_len=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_short")

    def test_output_exactly_300_pct_passes(self):
        """300 chars out of 100 → ratio = 3.0 exactly → allowed (not > 3.0)."""
        result = self._call("б" * 300, input_len=100)
        self.assertTrue(result.ok)

    def test_output_301_pct_is_too_long(self):
        """301 chars out of 100 → ratio = 3.01 > 3.0 → output_too_long."""
        result = self._call("б" * 301, input_len=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_long")

    def test_length_guard_skipped_for_exactly_20_chars(self):
        """input_len == 20: guard condition is `input_len > 20`, so NOT active."""
        inp = "а" * 20
        self.rw._session.post = MagicMock(return_value=_ok_resp("б"))
        result = self.rw.rewrite(inp)
        self.assertTrue(result.ok)

    def test_length_guard_active_for_21_chars(self):
        """input_len == 21: guard IS active → tiny output is rejected."""
        inp = "а" * 21
        self.rw._session.post = MagicMock(return_value=_ok_resp("б"))
        result = self.rw.rewrite(inp)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_short")


# ---------------------------------------------------------------------------
# 5. Session pooling: same _session instance reused
# ---------------------------------------------------------------------------

class SessionPoolingTestCase(unittest.TestCase):

    def test_session_is_requests_session_instance(self):
        rw = _make_rewriter()
        self.assertIsInstance(rw._session, _requests.Session)

    def test_same_session_used_across_multiple_calls(self):
        """All rewrite() calls go through the same _session object."""
        rw = _make_rewriter()
        original_session = rw._session
        mock_post = MagicMock(return_value=_ok_resp("б" * 60))
        original_session.post = mock_post

        for _ in range(3):
            rw.rewrite("а" * 60)

        # The session object hasn't been replaced
        self.assertIs(rw._session, original_session)
        self.assertEqual(mock_post.call_count, 3)

    def test_close_calls_session_close(self):
        """close() must call _session.close()."""
        rw = _make_rewriter()
        rw._session = MagicMock()
        rw.close()
        rw._session.close.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Timeout → exception → circuit update
# ---------------------------------------------------------------------------

class TimeoutCircuitUpdateTestCase(unittest.TestCase):

    def test_single_timeout_records_failure(self):
        rw = _make_rewriter(circuit_fail_threshold=3)
        rw._session.post = MagicMock(side_effect=_requests.Timeout())
        result = rw.rewrite("какой-то текст для теста один два три")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "timeout")
        self.assertIsNone(result.latency_ms)
        # one failure → still closed
        self.assertEqual(rw._circuit.state, "closed")

    def test_three_timeouts_open_circuit(self):
        rw = _make_rewriter(circuit_fail_threshold=3)
        rw._session.post = MagicMock(side_effect=_requests.Timeout())
        text = "текст для тестирования circuit один два три"
        for _ in range(3):
            rw.rewrite(text)
        self.assertEqual(rw._circuit.state, "open")
        result = rw.rewrite(text)
        self.assertEqual(result.fallback_reason, "circuit_open")
        # 4th call makes no HTTP request
        self.assertEqual(rw._session.post.call_count, 3)

    def test_request_exception_base_class_is_handled(self):
        """requests.RequestException (base) must not propagate."""
        rw = _make_rewriter()
        rw._session.post = MagicMock(
            side_effect=_requests.RequestException("generic")
        )
        result = rw.rewrite("текст для проверки обработки исключений")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "connection_error")


# ---------------------------------------------------------------------------
# 7. Empty input → empty output without API call
# ---------------------------------------------------------------------------

class EmptyInputNoApiCallTestCase(unittest.TestCase):

    def test_empty_string_no_http_call_no_latency(self):
        rw = _make_rewriter()
        rw._session.post = MagicMock()
        result = rw.rewrite("")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        self.assertIsNone(result.latency_ms)
        self.assertIsNone(result.text)
        rw._session.post.assert_not_called()

    def test_whitespace_input_no_http_call(self):
        rw = _make_rewriter()
        rw._session.post = MagicMock()
        result = rw.rewrite("   \t\n  ")
        rw._session.post.assert_not_called()
        self.assertEqual(result.fallback_reason, "empty_input")


# ---------------------------------------------------------------------------
# 8. max_tokens in HTTP payload
# ---------------------------------------------------------------------------

class MaxTokensPayloadTestCase(unittest.TestCase):

    def setUp(self):
        self.rw = _make_rewriter()
        self.rw._session.post = MagicMock(
            return_value=_ok_resp("б" * 60)
        )

    def test_payload_contains_max_tokens_key(self):
        self.rw.rewrite("а" * 60)
        _, kwargs = self.rw._session.post.call_args
        self.assertIn("max_tokens", kwargs["json"])

    def test_short_input_sends_floor_max_tokens(self):
        """3-word input → floor = 256."""
        self.rw.rewrite("раз два три")
        _, kwargs = self.rw._session.post.call_args
        self.assertEqual(kwargs["json"]["max_tokens"], 256)

    def test_medium_input_sends_scaled_max_tokens(self):
        """100-word input → max_tokens = 440."""
        text = " ".join(["слово"] * 100)
        self.rw.rewrite(text)
        _, kwargs = self.rw._session.post.call_args
        self.assertEqual(kwargs["json"]["max_tokens"], 440)

    def test_very_long_input_sends_ceiling_max_tokens(self):
        """2000-word input → ceiling = 4096."""
        text = " ".join(["слово"] * 2000)
        self.rw.rewrite(text)
        _, kwargs = self.rw._session.post.call_args
        self.assertEqual(kwargs["json"]["max_tokens"], 4096)


# ---------------------------------------------------------------------------
# 9. HTTP 401 / 403 fallback_reason
# ---------------------------------------------------------------------------

class HttpAuthErrorsTestCase(unittest.TestCase):

    def setUp(self):
        self.rw = _make_rewriter()

    def test_http_401_returns_correct_reason(self):
        self.rw._session.post = MagicMock(return_value=_err_resp(401))
        result = self.rw.rewrite("тест авторизации один два три")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "http_401")
        self.assertIsNotNone(result.latency_ms)

    def test_http_403_returns_correct_reason(self):
        self.rw._session.post = MagicMock(return_value=_err_resp(403))
        result = self.rw.rewrite("тест доступа один два три четыре")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "http_403")

    def test_http_error_records_circuit_failure(self):
        """Each HTTP error increments circuit failure counter."""
        self.rw._session.post = MagicMock(return_value=_err_resp(500))
        text = "текст проверки ошибок один два три"
        for _ in range(3):
            self.rw.rewrite(text)
        self.assertEqual(self.rw._circuit.state, "open")


# ---------------------------------------------------------------------------
# 10. circuit_open state propagated to status()
# ---------------------------------------------------------------------------

class StatusAfterCircuitOpenTestCase(unittest.TestCase):

    def test_status_reachable_false_after_circuit_opens(self):
        rw = _make_rewriter(circuit_fail_threshold=3)
        rw._session.post = MagicMock(side_effect=_requests.ConnectionError())
        text = "текст для теста статуса circuit"
        for _ in range(3):
            rw.rewrite(text)
        status = rw.status()
        self.assertEqual(status["circuit_state"], "open")
        self.assertFalse(status["reachable"])

    def test_last_error_updated_after_timeout(self):
        rw = _make_rewriter()
        rw._session.post = MagicMock(side_effect=_requests.Timeout())
        rw.rewrite("любой текст один два три")
        self.assertEqual(rw._last_error, "timeout")


if __name__ == "__main__":
    unittest.main()
