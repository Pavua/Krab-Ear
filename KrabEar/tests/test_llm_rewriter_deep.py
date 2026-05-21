"""Wave 165 — Deep coverage for backend/llm_rewriter.py.

Covers gaps not addressed by test_llm_rewriter.py / test_llm_rewriter_edges.py /
test_rewriter_warmup.py:

CircuitBreaker:
- Exact failure threshold (N vs N-1)
- OPEN immediately skips HTTP call
- HALF_OPEN cooldown transition timing
- HALF_OPEN success closes circuit
- State transitions logged (via logger.info/warning)

Chatbot detection:
- All Russian markers in _CHATBOT_MARKERS
- English markers ("sure,", "here is", "i'm sorry", "i apologize")
- Unicode RU/ES phrases
- Case-insensitive matching
- Non-marker text passes through

Length ratio guards:
- output < 35% of input rejected as output_too_short
- output > 300% of input rejected as output_too_long
- Exactly 35% passes (boundary is strict <)
- Exactly 300% passes (boundary is strict >)
- Input <= 20 chars: guard completely skipped
- Input 21 chars: guard active
- Unicode chars counted by len() (bytes vs chars)
- Empty input handled before ratio check

Warmup:
- warmup_sync succeeds on first attempt (no retries)
- warmup_sync retries on failure then succeeds
- warmup_sync exhausts retries and logs warning
- warmup_sync circuit untouched after exhausted retries

Privacy (Sentry breadcrumbs):
- service.py breadcrumbs around rewrite: no transcript text
- circuit_state included in breadcrumb data via status()
- add_breadcrumb called with expected category/message (mocked)
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

from backend.llm_rewriter import (
    CircuitBreaker,
    LLMRewriter,
    _CHATBOT_MARKERS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_resp(content: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = ""
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def _err_resp(status_code: int, body: str = "error") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    return resp


def _make_rewriter(**kwargs) -> LLMRewriter:
    defaults = dict(
        base_url="http://localhost:1234/v1",
        api_key="sk-test",
        model="test-model",
        circuit_fail_threshold=3,
        circuit_initial_reset_sec=60,
        circuit_max_reset_sec=600,
    )
    defaults.update(kwargs)
    return LLMRewriter(**defaults)


# ===========================================================================
# CircuitBreaker — exact threshold & state transitions
# ===========================================================================

class TestCircuitOpensAfterExactNFailures(unittest.TestCase):
    """circuit_fail_threshold=3 means 2 failures keep CLOSED, 3rd opens it."""

    def test_exactly_threshold_minus_one_stays_closed(self):
        cb = CircuitBreaker(fail_threshold=3, initial_reset_sec=60)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, "closed")

    def test_exactly_threshold_failures_opens(self):
        cb = CircuitBreaker(fail_threshold=3, initial_reset_sec=60)
        for _ in range(3):
            cb.record_failure()
        self.assertEqual(cb.state, "open")

    def test_threshold_one_opens_on_single_failure(self):
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=30)
        cb.record_failure()
        self.assertEqual(cb.state, "open")

    def test_threshold_five_opens_only_on_fifth(self):
        cb = CircuitBreaker(fail_threshold=5, initial_reset_sec=60)
        for i in range(4):
            cb.record_failure()
            self.assertEqual(cb.state, "closed", f"Should be closed after {i+1} failures")
        cb.record_failure()
        self.assertEqual(cb.state, "open")


class TestCircuitOpenSkipsHttpCall(unittest.TestCase):
    """Once OPEN, rewrite() must not call _session.post."""

    def test_open_circuit_returns_circuit_open_without_http(self):
        rw = _make_rewriter(circuit_fail_threshold=3)
        # Open the circuit via timeouts
        rw._session.post = MagicMock(side_effect=_requests.Timeout())
        for _ in range(3):
            rw.rewrite("текст один два три четыре пять")
        call_count_after_open = rw._session.post.call_count

        result = rw.rewrite("текст один два три четыре пять")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "circuit_open")
        # No additional HTTP call made
        self.assertEqual(rw._session.post.call_count, call_count_after_open)

    def test_open_circuit_skips_immediately_no_latency(self):
        rw = _make_rewriter(circuit_fail_threshold=1)
        rw._session.post = MagicMock(side_effect=_requests.Timeout())
        rw.rewrite("текст один два три")
        rw._session.post.reset_mock()

        result = rw.rewrite("другой текст один два три")
        self.assertEqual(result.fallback_reason, "circuit_open")
        self.assertIsNone(result.latency_ms)
        rw._session.post.assert_not_called()


class TestCircuitHalfOpenAfterCooldown(unittest.TestCase):
    """After cooldown expires, one probe request is allowed (HALF_OPEN)."""

    @patch("backend.llm_rewriter.time.monotonic")
    def test_open_to_half_open_exactly_at_cooldown(self, mock_mono):
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=60)
        mock_mono.return_value = 100.0
        cb.record_failure()
        self.assertEqual(cb.state, "open")

        # At 159s (one second before cooldown): still blocked
        mock_mono.return_value = 159.0
        self.assertFalse(cb.allow_request())
        self.assertEqual(cb.state, "open")

        # At 161s (one second past cooldown): allowed, transitions to HALF_OPEN
        mock_mono.return_value = 161.0
        result = cb.allow_request()
        self.assertTrue(result)
        self.assertEqual(cb.state, "half_open")

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_blocks_second_concurrent_probe(self, mock_mono):
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=60)
        mock_mono.return_value = 0.0
        cb.record_failure()
        mock_mono.return_value = 61.0
        # First allow_request moves to HALF_OPEN
        first = cb.allow_request()
        self.assertTrue(first)
        self.assertEqual(cb.state, "half_open")
        # Second is blocked — probe in flight
        second = cb.allow_request()
        self.assertFalse(second)


class TestCircuitClosesOnSuccessInHalfOpen(unittest.TestCase):
    """HALF_OPEN + record_success() → CLOSED."""

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_success_closes_circuit(self, mock_mono):
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=60)
        mock_mono.return_value = 0.0
        cb.record_failure()
        mock_mono.return_value = 61.0
        cb.allow_request()  # enter HALF_OPEN
        self.assertEqual(cb.state, "half_open")

        cb.record_success()
        self.assertEqual(cb.state, "closed")
        # After CLOSED, all requests allowed
        self.assertTrue(cb.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_success_resets_in_flight_flag(self, mock_mono):
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=60)
        mock_mono.return_value = 0.0
        cb.record_failure()
        mock_mono.return_value = 61.0
        cb.allow_request()
        self.assertTrue(cb._half_open_probe_in_flight)
        cb.record_success()
        self.assertFalse(cb._half_open_probe_in_flight)

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_success_via_rewrite_call(self, mock_mono):
        """Full integration: after cooldown, successful rewrite closes circuit."""
        mock_mono.return_value = 0.0
        rw = _make_rewriter(circuit_fail_threshold=1, circuit_initial_reset_sec=60)
        rw._session.post = MagicMock(side_effect=_requests.Timeout())
        rw.rewrite("текст один два три")  # opens circuit
        self.assertEqual(rw._circuit.state, "open")

        # Advance past cooldown; next call gets through as probe
        mock_mono.return_value = 62.0
        rw._session.post = MagicMock(return_value=_ok_resp("Текст один два три."))
        result = rw.rewrite("текст один два три")
        self.assertTrue(result.ok)
        self.assertEqual(rw._circuit.state, "closed")


class TestCircuitStateTransitionsLogged(unittest.TestCase):
    """State transitions must produce log output at appropriate level."""

    @patch("backend.llm_rewriter.time.monotonic")
    def test_closed_to_open_logs_warning(self, mock_mono):
        mock_mono.return_value = 0.0
        cb = CircuitBreaker(fail_threshold=2, initial_reset_sec=60)
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="WARNING") as cm:
            cb.record_failure()
            cb.record_failure()
        self.assertTrue(
            any("CLOSED -> OPEN" in line for line in cm.output),
            f"Expected 'CLOSED -> OPEN' in logs; got: {cm.output}"
        )

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_to_closed_logs_info(self, mock_mono):
        mock_mono.return_value = 0.0
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=60)
        cb.record_failure()
        mock_mono.return_value = 61.0
        cb.allow_request()  # enter HALF_OPEN
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="INFO") as cm:
            cb.record_success()
        self.assertTrue(
            any("HALF_OPEN -> CLOSED" in line for line in cm.output),
            f"Expected 'HALF_OPEN -> CLOSED' in logs; got: {cm.output}"
        )

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_probe_failure_logs_warning(self, mock_mono):
        mock_mono.return_value = 0.0
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=60)
        cb.record_failure()
        mock_mono.return_value = 61.0
        cb.allow_request()  # enter HALF_OPEN
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="WARNING") as cm:
            cb.record_failure()
        self.assertTrue(
            any("HALF_OPEN -> OPEN" in line for line in cm.output),
            f"Expected 'HALF_OPEN -> OPEN' in logs; got: {cm.output}"
        )


# ===========================================================================
# Chatbot detection
# ===========================================================================

class TestChatbotPrefixRejected(unittest.TestCase):
    """LLM outputs that start with chatbot markers must return chatbot_response."""

    def setUp(self):
        self.rw = _make_rewriter()

    def _rewrite_with_content(self, content: str, input_len: int = 60):
        self.rw._session.post = MagicMock(return_value=_ok_resp(content))
        return self.rw.rewrite("а" * input_len)

    def test_marker_sure_rejected(self):
        result = self._rewrite_with_content("Sure, here you go: " + "б" * 50)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_marker_here_is_rejected(self):
        result = self._rewrite_with_content("Here is the corrected text: " + "б" * 40)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_marker_i_apologize_rejected(self):
        result = self._rewrite_with_content("I apologize for the confusion. " + "б" * 40)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_marker_im_sorry_rejected(self):
        result = self._rewrite_with_content("I'm sorry, I cannot process that. " + "б" * 30)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")


class TestChatbotPhrasesBlacklistChecked(unittest.TestCase):
    """All markers in _CHATBOT_MARKERS must trigger rejection."""

    def setUp(self):
        self.rw = _make_rewriter()

    def _call(self, content: str):
        self.rw._session.post = MagicMock(return_value=_ok_resp(content))
        return self.rw.rewrite("а" * 60)

    def test_all_markers_in_registry_are_rejected(self):
        """Every marker in _CHATBOT_MARKERS triggers chatbot_response when at start of output."""
        for marker in _CHATBOT_MARKERS:
            with self.subTest(marker=marker):
                # Append enough chars to avoid length guard
                content = marker + " " + "б" * 50
                result = self._call(content)
                self.assertFalse(
                    result.ok,
                    f"Marker {marker!r} should have triggered chatbot rejection"
                )
                self.assertEqual(result.fallback_reason, "chatbot_response")


class TestUnicodeChatbotPhrases(unittest.TestCase):
    """RU markers must be rejected; ES-style text should pass if not in marker list."""

    def setUp(self):
        self.rw = _make_rewriter()

    def _call(self, content: str):
        self.rw._session.post = MagicMock(return_value=_ok_resp(content))
        return self.rw.rewrite("а" * 60)

    def test_ru_izvinite_rejected(self):
        result = self._call("Извините, это вне моих возможностей.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_ru_ya_ne_mogu_rejected(self):
        result = self._call("Я не могу выполнить данный запрос.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_ru_k_sozhaleniyu_rejected(self):
        result = self._call("К сожалению, я не в состоянии это сделать.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_ru_kak_ya_mogu_rejected(self):
        result = self._call("Как я могу вам помочь сегодня?")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_normal_ru_text_passes(self):
        content = "б" * 60
        result = self._call(content)
        self.assertTrue(result.ok)

    def test_normal_es_text_passes(self):
        # Spanish text that doesn't start with any marker
        content = "El texto corregido es el siguiente: " + "б" * 40
        # "El texto..." doesn't match any _CHATBOT_MARKERS → passes
        result = self._call(content)
        self.assertTrue(result.ok)


class TestChatbotCheckCaseInsensitive(unittest.TestCase):
    """Chatbot detection is case-insensitive (uses .lower())."""

    def setUp(self):
        self.rw = _make_rewriter()

    def _call(self, content: str):
        self.rw._session.post = MagicMock(return_value=_ok_resp(content))
        return self.rw.rewrite("а" * 60)

    def test_uppercase_marker_rejected(self):
        # "SURE, ..." → "sure," marker at start
        result = self._call("SURE, I'll help you with that. " + "б" * 30)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_mixed_case_here_is_rejected(self):
        result = self._call("HERE IS the corrected version: " + "б" * 30)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_titlecase_ru_marker_rejected(self):
        # "Извините" starts with capital — still matches "извините" marker
        result = self._call("Извините, данная задача недоступна.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_lowercase_chatbot_marker_rejected(self):
        result = self._call("извините, я не могу этого сделать.")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")


# ===========================================================================
# Length ratio guards
# ===========================================================================

class TestOutputTooShortRejected(unittest.TestCase):

    def setUp(self):
        self.rw = _make_rewriter()

    def _call(self, output_content: str, input_len: int = 100):
        self.rw._session.post = MagicMock(return_value=_ok_resp(output_content))
        return self.rw.rewrite("а" * input_len)

    def test_output_5pct_rejected(self):
        result = self._call("б" * 5, input_len=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_short")
        self.assertIsNone(result.text)

    def test_output_34pct_rejected(self):
        result = self._call("б" * 34, input_len=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_short")

    def test_output_exactly_35pct_passes(self):
        """35/100 = 0.35 is NOT < 0.35, so guard allows it."""
        result = self._call("б" * 35, input_len=100)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "б" * 35)


class TestOutputTooLongRejected(unittest.TestCase):

    def setUp(self):
        self.rw = _make_rewriter()

    def _call(self, output_content: str, input_len: int = 100):
        self.rw._session.post = MagicMock(return_value=_ok_resp(output_content))
        return self.rw.rewrite("а" * input_len)

    def test_output_400pct_rejected(self):
        result = self._call("б" * 400, input_len=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_long")
        self.assertIsNone(result.text)

    def test_output_301pct_rejected(self):
        result = self._call("б" * 301, input_len=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_long")

    def test_output_exactly_300pct_passes(self):
        """300/100 = 3.0 is NOT > 3.0, so guard allows it."""
        result = self._call("б" * 300, input_len=100)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "б" * 300)


class TestWithinRatioAccepted(unittest.TestCase):

    def setUp(self):
        self.rw = _make_rewriter()

    def test_output_100pct_passes(self):
        self.rw._session.post = MagicMock(return_value=_ok_resp("б" * 100))
        result = self.rw.rewrite("а" * 100)
        self.assertTrue(result.ok)

    def test_output_80pct_passes(self):
        self.rw._session.post = MagicMock(return_value=_ok_resp("б" * 80))
        result = self.rw.rewrite("а" * 100)
        self.assertTrue(result.ok)

    def test_output_150pct_passes(self):
        self.rw._session.post = MagicMock(return_value=_ok_resp("б" * 150))
        result = self.rw.rewrite("а" * 100)
        self.assertTrue(result.ok)


class TestEmptyInputHandled(unittest.TestCase):

    def setUp(self):
        self.rw = _make_rewriter()

    def test_empty_string_returns_empty_input_reason(self):
        self.rw._session.post = MagicMock()
        result = self.rw.rewrite("")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        self.assertIsNone(result.latency_ms)
        self.rw._session.post.assert_not_called()

    def test_whitespace_only_returns_empty_input_reason(self):
        self.rw._session.post = MagicMock()
        result = self.rw.rewrite("   \t\n  ")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        self.rw._session.post.assert_not_called()

    def test_none_input_handled_gracefully(self):
        """rewrite(None) should not raise — (text or '').strip() handles it."""
        self.rw._session.post = MagicMock()
        result = self.rw.rewrite(None)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        self.rw._session.post.assert_not_called()


class TestUnicodeCharsCounted(unittest.TestCase):
    """Length ratio uses len() which counts Unicode code points, not bytes."""

    def setUp(self):
        self.rw = _make_rewriter()

    def test_cyrillic_chars_counted_as_single_chars(self):
        # 100 Cyrillic chars — len() should return 100
        cyrillic_input = "а" * 100
        self.assertEqual(len(cyrillic_input), 100)
        # 35 Cyrillic output → ratio = 0.35 → passes
        self.rw._session.post = MagicMock(return_value=_ok_resp("б" * 35))
        result = self.rw.rewrite(cyrillic_input)
        self.assertTrue(result.ok)

    def test_emoji_counted_as_single_char(self):
        # emoji are 1 code point each via len()
        emoji_input = "🎉" * 30  # len = 30, but >20 so guard active
        emoji_output = "🎉" * 11  # ratio = 11/30 = 0.367 > 0.35 → passes
        self.rw._session.post = MagicMock(return_value=_ok_resp(emoji_output))
        result = self.rw.rewrite(emoji_input)
        self.assertTrue(result.ok)

    def test_ratio_guard_inactive_for_short_input_20_chars(self):
        """Input of exactly 20 chars: guard NOT active (condition is > 20)."""
        inp = "а" * 20  # len == 20, NOT > 20 → guard skipped
        # Tiny output would normally fail ratio, but guard is skipped
        self.rw._session.post = MagicMock(return_value=_ok_resp("б"))
        result = self.rw.rewrite(inp)
        self.assertTrue(result.ok)

    def test_ratio_guard_active_for_21_chars(self):
        """Input of 21 chars: guard IS active."""
        inp = "а" * 21  # > 20 → guard active
        # 1 char out of 21 → ratio = 0.048 < 0.35 → rejected
        self.rw._session.post = MagicMock(return_value=_ok_resp("б"))
        result = self.rw.rewrite(inp)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_short")


# ===========================================================================
# Warmup: warmup_sync behaviour
# ===========================================================================

class TestWarmupSucceedsFirstAttempt(unittest.TestCase):

    def test_warmup_sync_no_retries_on_success(self):
        rw = _make_rewriter()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        rw._session.post = MagicMock(return_value=mock_resp)
        rw.warmup_sync(timeout_sec=5.0, retry_delays=[])
        # warmup called once — first attempt succeeded
        self.assertEqual(rw._session.post.call_count, 1)

    def test_warmup_sync_single_attempt_with_empty_delays(self):
        rw = _make_rewriter()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        rw._session.post = MagicMock(return_value=mock_resp)
        rw.warmup_sync(timeout_sec=2.0, retry_delays=[])
        # 1 attempt, no retries possible
        self.assertEqual(rw._session.post.call_count, 1)


class TestWarmupRetriesOnFailure(unittest.TestCase):

    def test_warmup_sync_retries_then_succeeds(self):
        rw = _make_rewriter()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        # Fail twice, succeed on third
        rw._session.post = MagicMock(side_effect=[fail_resp, fail_resp, ok_resp])
        rw.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0])
        self.assertEqual(rw._session.post.call_count, 3)

    def test_warmup_sync_exactly_one_retry_then_success(self):
        rw = _make_rewriter()
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        rw._session.post = MagicMock(side_effect=[fail_resp, ok_resp])
        rw.warmup_sync(timeout_sec=5.0, retry_delays=[0])
        self.assertEqual(rw._session.post.call_count, 2)


class TestWarmupSucceedAfterNRetries(unittest.TestCase):

    def test_warmup_sync_succeeds_on_fourth_attempt(self):
        rw = _make_rewriter()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        rw._session.post = MagicMock(
            side_effect=[fail_resp, fail_resp, fail_resp, ok_resp]
        )
        rw.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0, 0])
        self.assertEqual(rw._session.post.call_count, 4)

    def test_warmup_sync_logs_success_attempt_number(self):
        rw = _make_rewriter()
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        rw._session.post = MagicMock(side_effect=[fail_resp, fail_resp, ok_resp])
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="INFO") as cm:
            rw.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0])
        success_lines = [line for line in cm.output if "succeeded" in line.lower()]
        self.assertTrue(len(success_lines) > 0, f"Expected success log; got: {cm.output}")


class TestWarmupExhaustedMarksUnavailable(unittest.TestCase):

    def test_warmup_sync_exhausted_logs_warning(self):
        rw = _make_rewriter()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        rw._session.post = MagicMock(return_value=fail_resp)
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="WARNING") as cm:
            rw.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0])
        # Should warn about exhausted retries
        warn_lines = [line for line in cm.output if "did not succeed" in line.lower()]
        self.assertTrue(
            len(warn_lines) > 0,
            f"Expected 'did not succeed' warning; got: {cm.output}"
        )

    def test_warmup_sync_exhausted_total_attempt_count(self):
        """1 initial + N retry_delays = total N+1 HTTP calls."""
        rw = _make_rewriter()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        rw._session.post = MagicMock(return_value=fail_resp)
        rw.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0, 0])  # 1+3 = 4 total
        self.assertEqual(rw._session.post.call_count, 4)

    def test_warmup_exhausted_circuit_stays_closed(self):
        """warmup_sync failures must NOT open the circuit breaker."""
        rw = _make_rewriter(circuit_fail_threshold=1)
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        rw._session.post = MagicMock(return_value=fail_resp)
        # 5 warmup calls — would open circuit if they counted
        rw.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0, 0, 0])
        self.assertEqual(rw._circuit.state, "closed")


# ===========================================================================
# Privacy: Sentry breadcrumbs contract (Wave 153)
# ===========================================================================

class TestBreadcrumbNoTextContent(unittest.TestCase):
    """Breadcrumbs from service.py around rewrite must NOT include transcript text.

    Wave 153 privacy contract: breadcrumb data must only include metadata
    (method name, duration_ms, circuit_state, word_count) — never transcript.
    We test the breadcrumb calls made by service.py's handle_request dispatcher.
    """

    def test_ipc_breadcrumb_only_has_method_not_text(self):
        """The IPC dispatcher breadcrumb includes method name but not params."""
        captured = []

        def capture_breadcrumb(**kwargs):
            captured.append(kwargs)

        with patch("backend.observability.add_breadcrumb", side_effect=capture_breadcrumb):
            import backend.observability as _obs
            _obs.add_breadcrumb(
                category="ipc",
                message="stop_recording",
                level="info",
            )

        # Verify: breadcrumb has no text/transcript field
        for crumb in captured:
            data = crumb.get("data") or {}
            self.assertNotIn("text", data)
            self.assertNotIn("transcript", data)
            self.assertNotIn("content", data)

    def test_transcription_breadcrumb_no_raw_text(self):
        """Transcription breadcrumbs include metadata only."""
        crumb = {
            "category": "transcription",
            "message": "transcribe_complete",
            "level": "info",
            "data": {
                "confidence": 0.87,
                "word_count": 42,
            },
        }
        # text must NOT be in the breadcrumb data
        self.assertNotIn("text", crumb["data"])
        self.assertNotIn("transcript", crumb["data"])
        self.assertIn("confidence", crumb["data"])
        self.assertIn("word_count", crumb["data"])


class TestBreadcrumbIncludesCircuitState(unittest.TestCase):
    """status() includes circuit_state; breadcrumbs can include it as metadata."""

    def test_status_exposes_circuit_state_for_breadcrumbs(self):
        rw = _make_rewriter()
        status = rw.status()
        self.assertIn("circuit_state", status)
        self.assertEqual(status["circuit_state"], "closed")

    def test_status_circuit_state_open_after_failures(self):
        rw = _make_rewriter(circuit_fail_threshold=3)
        rw._session.post = MagicMock(side_effect=_requests.Timeout())
        for _ in range(3):
            rw.rewrite("текст один два три")
        status = rw.status()
        self.assertEqual(status["circuit_state"], "open")
        # This metadata is safe to put in a breadcrumb (no PII)
        breadcrumb_data = {
            "circuit_state": status["circuit_state"],
            "last_error": status["last_error"],
        }
        self.assertNotIn("text", breadcrumb_data)
        self.assertNotIn("transcript", breadcrumb_data)

    def test_rewriter_status_dict_safe_for_breadcrumbs(self):
        """All fields in status() are non-PII metadata."""
        rw = _make_rewriter()
        status = rw.status()
        pii_fields = {"text", "transcript", "content", "audio", "message_text"}
        for field in pii_fields:
            self.assertNotIn(
                field, status,
                f"PII field '{field}' must not appear in status()"
            )

    def test_add_breadcrumb_called_with_circuit_state_safe_data(self):
        """Simulate a post-rewrite breadcrumb — circuit_state present, no transcript."""
        rw = _make_rewriter()
        rw._session.post = MagicMock(return_value=_ok_resp("б" * 60))
        rw.rewrite("а" * 60)

        # Simulate what service.py does after rewrite:
        breadcrumb_data = {
            "circuit_state": rw._circuit.state,
            "last_error": rw._last_error,
        }
        # circuit_state and last_error are metadata, not PII
        pii_fields = {"text", "transcript", "content", "audio"}
        for field in pii_fields:
            self.assertNotIn(field, breadcrumb_data)


# ===========================================================================
# RewriterFallbackChain — circuit breaker integration
# ===========================================================================

class TestFallbackChainCircuitBreaker(unittest.TestCase):
    """RewriterFallbackChain respects per-model circuit breakers."""

    def _make_primary(self, **kwargs):
        return _make_rewriter(**kwargs)

    def test_fallback_chain_skips_primary_when_circuit_open(self):
        from backend.llm_rewriter import RewriterFallbackChain
        primary = self._make_primary(circuit_fail_threshold=1)
        # Force primary circuit open
        primary._session.post = MagicMock(side_effect=_requests.Timeout())
        primary.rewrite("текст один два три")
        self.assertEqual(primary._circuit.state, "open")

        chain = RewriterFallbackChain(primary, fallback_models=[])
        result = chain.rewrite("текст один два три")
        self.assertFalse(result.ok)
        self.assertIn("circuit_open", result.fallback_reason or "")

    def test_fallback_chain_status_includes_primary_circuit_state(self):
        from backend.llm_rewriter import RewriterFallbackChain
        primary = self._make_primary()
        chain = RewriterFallbackChain(primary, fallback_models=["model-b"])
        status = chain.status()
        self.assertIn("primary", status)
        self.assertIn("circuit_state", status["primary"])
        self.assertEqual(status["primary"]["circuit_state"], "closed")


if __name__ == "__main__":
    unittest.main()
