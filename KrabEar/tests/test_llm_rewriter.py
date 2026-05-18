"""Unit tests для backend/llm_rewriter.py — CircuitBreaker + LLMRewriter."""

import os
import unittest
from unittest.mock import patch, MagicMock

import requests


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


class LLMRewriteResultTestCase(unittest.TestCase):
    def test_ok_result_returns_text(self):
        from backend.llm_rewriter import LLMRewriteResult
        r = LLMRewriteResult(ok=True, text="clean", fallback_reason=None, latency_ms=100)
        self.assertEqual(r.text_or_fallback("raw"), "clean")

    def test_failed_result_returns_fallback(self):
        from backend.llm_rewriter import LLMRewriteResult
        r = LLMRewriteResult(ok=False, text=None, fallback_reason="timeout", latency_ms=None)
        self.assertEqual(r.text_or_fallback("raw"), "raw")

    def test_ok_but_none_text_returns_fallback(self):
        """Edge case: ok=True но text=None (не должно случаться, но защищаемся)."""
        from backend.llm_rewriter import LLMRewriteResult
        r = LLMRewriteResult(ok=True, text=None, fallback_reason=None, latency_ms=100)
        self.assertEqual(r.text_or_fallback("raw"), "raw")


class LLMRewriterPostprocessTestCase(unittest.TestCase):
    """Тесты приватного _postprocess метода."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_strips_double_quotes(self):
        self.assertEqual(self.rewriter._postprocess('"Привет, мир."'), "Привет, мир.")

    def test_strips_french_quotes(self):
        self.assertEqual(self.rewriter._postprocess("«Привет, мир.»"), "Привет, мир.")

    def test_strips_curly_quotes(self):
        self.assertEqual(self.rewriter._postprocess("\u201cПривет, мир.\u201d"), "Привет, мир.")

    def test_strips_explanatory_prefix_ispravlenny(self):
        self.assertEqual(
            self.rewriter._postprocess("Исправленный текст: Привет, мир."),
            "Привет, мир.",
        )

    def test_strips_explanatory_prefix_ispravleno(self):
        self.assertEqual(
            self.rewriter._postprocess("Исправлено: Привет, мир."),
            "Привет, мир.",
        )

    def test_strips_explanatory_prefix_case_insensitive(self):
        self.assertEqual(
            self.rewriter._postprocess("исправленный текст: Привет, мир."),
            "Привет, мир.",
        )

    def test_takes_first_paragraph_on_double_newline(self):
        self.assertEqual(
            self.rewriter._postprocess("Привет, мир.\n\n**Пояснение**: я убрал запятую."),
            "Привет, мир.",
        )

    def test_empty_string_stays_empty(self):
        self.assertEqual(self.rewriter._postprocess(""), "")

    def test_whitespace_only_stays_empty(self):
        self.assertEqual(self.rewriter._postprocess("   \n  "), "")

    def test_passes_through_normal_text(self):
        self.assertEqual(
            self.rewriter._postprocess("Привет, как дела?"),
            "Привет, как дела?",
        )


class LLMRewriterMaxTokensTestCase(unittest.TestCase):
    """Тесты dynamic max_tokens estimator."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_short_text_hits_floor(self):
        """Короткий текст (5 слов) → max_tokens = 256 (floor)."""
        result = self.rewriter._estimate_max_tokens("Привет как дела мой друг")
        self.assertEqual(result, 256)

    def test_medium_text_scales_linearly(self):
        """100 слов → примерно 100 * 3 * 1.3 + 50 = 440."""
        text = " ".join(["слово"] * 100)
        result = self.rewriter._estimate_max_tokens(text)
        self.assertEqual(result, 440)

    def test_long_text_hits_ceiling(self):
        """2000 слов → max_tokens = 4096 (ceiling)."""
        text = " ".join(["слово"] * 2000)
        result = self.rewriter._estimate_max_tokens(text)
        self.assertEqual(result, 4096)

    def test_empty_text_returns_floor(self):
        result = self.rewriter._estimate_max_tokens("")
        self.assertEqual(result, 256)


class LLMRewriterRewriteSuccessTestCase(unittest.TestCase):
    """Happy path tests для rewrite()."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=4.0,
        )

    def _mock_response(self, content: str, status_code: int = 200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = {
            "choices": [
                {"message": {"content": content}}
            ]
        }
        return mock_resp

    def test_successful_rewrite_returns_ok_result(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_response("Привет, мир."))
        result = self.rewriter.rewrite("привет мир")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Привет, мир.")
        self.assertIsNone(result.fallback_reason)
        self.assertIsNotNone(result.latency_ms)

    def test_rewrite_calls_correct_endpoint(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_response("ok"))
        self.rewriter.rewrite("test")
        args, kwargs = self.rewriter._session.post.call_args
        self.assertEqual(args[0], "http://localhost:1234/v1/chat/completions")

    def test_rewrite_sends_correct_payload(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_response("ok"))
        self.rewriter.rewrite("test input")
        kwargs = self.rewriter._session.post.call_args.kwargs
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["stream"], False)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertEqual(payload["messages"][1]["content"], "test input")

    def test_rewrite_sends_authorization_header(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_response("ok"))
        self.rewriter.rewrite("test")
        kwargs = self.rewriter._session.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_rewrite_strips_quotes_from_response(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_response('"Привет, мир."'))
        result = self.rewriter.rewrite("привет мир")
        self.assertEqual(result.text, "Привет, мир.")

    def test_empty_input_returns_empty_input_without_http_call(self):
        self.rewriter._session.post = MagicMock()
        result = self.rewriter.rewrite("")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        self.rewriter._session.post.assert_not_called()

    def test_whitespace_only_input_returns_empty_input(self):
        self.rewriter._session.post = MagicMock()
        result = self.rewriter.rewrite("   \n  ")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        self.rewriter._session.post.assert_not_called()


class LLMRewriterRewriteFailuresTestCase(unittest.TestCase):
    """Failure mode tests: timeout, connection, HTTP errors, parse errors."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_timeout_returns_fallback_and_records_failure(self):
        import requests
        self.rewriter._session.post = MagicMock(side_effect=requests.Timeout("timeout"))
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "timeout")

    def test_connection_error_returns_fallback(self):
        import requests
        self.rewriter._session.post = MagicMock(side_effect=requests.ConnectionError("refused"))
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "connection_error")

    def test_http_500_returns_fallback(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        self.rewriter._session.post = MagicMock(return_value=mock_resp)
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "http_500")

    def test_malformed_json_returns_parse_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        self.rewriter._session.post = MagicMock(return_value=mock_resp)
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "parse_error")

    def test_missing_choices_returns_parse_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"error": "no choices"}
        self.rewriter._session.post = MagicMock(return_value=mock_resp)
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "parse_error")

    def test_empty_content_returns_empty_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": ""}}]}
        self.rewriter._session.post = MagicMock(return_value=mock_resp)
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_response")

    def test_circuit_opens_after_three_consecutive_failures(self):
        import requests
        self.rewriter._session.post = MagicMock(side_effect=requests.ConnectionError("refused"))
        for _ in range(3):
            self.rewriter.rewrite("test")
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "circuit_open")
        self.assertEqual(self.rewriter._session.post.call_count, 3)


class LLMRewriterCircuitIntegrationTestCase(unittest.TestCase):
    """Integration: circuit breaker не блокирует запросы при empty_input."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_empty_input_does_not_count_as_failure(self):
        for _ in range(5):
            self.rewriter.rewrite("")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        self.rewriter._session.post = MagicMock(return_value=mock_resp)
        result = self.rewriter.rewrite("real text")
        self.assertTrue(result.ok)


class LLMRewriterPingTestCase(unittest.TestCase):
    """Тесты ping() health check метода."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=2.0,
        )

    def test_ping_returns_true_on_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        self.rewriter._session.get = MagicMock(return_value=mock_resp)
        self.assertTrue(self.rewriter.ping())

    def test_ping_returns_false_on_connection_error(self):
        import requests
        self.rewriter._session.get = MagicMock(side_effect=requests.ConnectionError("refused"))
        self.assertFalse(self.rewriter.ping())

    def test_ping_returns_false_on_timeout(self):
        import requests
        self.rewriter._session.get = MagicMock(side_effect=requests.Timeout())
        self.assertFalse(self.rewriter.ping())

    def test_ping_returns_false_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        self.rewriter._session.get = MagicMock(return_value=mock_resp)
        self.assertFalse(self.rewriter.ping())

    def test_ping_uses_models_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        self.rewriter._session.get = MagicMock(return_value=mock_resp)
        self.rewriter.ping()
        args, _ = self.rewriter._session.get.call_args
        self.assertEqual(args[0], "http://localhost:1234/v1/models")


class LLMRewriterStatusTestCase(unittest.TestCase):
    """Тесты status() diagnostic метода."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="qwen3.5-9b@6bit",
        )

    def test_status_returns_dict_with_required_keys(self):
        status = self.rewriter.status()
        self.assertIn("reachable", status)
        self.assertIn("model", status)
        self.assertIn("circuit_state", status)
        self.assertIn("last_latency_ms", status)
        self.assertIn("last_error", status)

    def test_status_model_matches_init(self):
        status = self.rewriter.status()
        self.assertEqual(status["model"], "qwen3.5-9b@6bit")

    def test_status_initial_circuit_state_is_closed(self):
        status = self.rewriter.status()
        self.assertEqual(status["circuit_state"], "closed")

    def test_status_initial_last_error_is_none(self):
        status = self.rewriter.status()
        self.assertIsNone(status["last_error"])

    @patch("backend.llm_rewriter.requests.post")
    def test_status_reachable_true_when_circuit_closed(self, mock_post):
        status = self.rewriter.status()
        self.assertTrue(status["reachable"])

    @patch("backend.llm_rewriter.requests.post")
    def test_status_reachable_false_when_circuit_open(self, mock_post):
        import requests
        mock_post.side_effect = requests.ConnectionError()
        for _ in range(3):
            self.rewriter.rewrite("test")
        status = self.rewriter.status()
        self.assertEqual(status["circuit_state"], "open")
        self.assertFalse(status["reachable"])


class LLMRewriterChatbotGuardTestCase(unittest.TestCase):
    """test_chatbot_guard_rejects — ответы в режиме ассистента отклоняются."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def _mock_resp(self, content: str):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": content}}]}
        return mock_resp

    def test_chatbot_guard_rejects_sure(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp("Sure, here is the corrected text: привет."))
        result = self.rewriter.rewrite("привет мир тест строка")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")
        self.assertIsNone(result.text)

    def test_chatbot_guard_rejects_here_is(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp("Here is the corrected version: текст."))
        result = self.rewriter.rewrite("некоторый текст для проверки guard")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_chatbot_guard_rejects_k_sozhaleniyu(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp("К сожалению, я не могу это исправить."))
        result = self.rewriter.rewrite("какой-то текст для обработки guard")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")

    def test_chatbot_guard_passes_normal_correction(self):
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp("Привет, мир! Как дела?"))
        result = self.rewriter.rewrite("привет мир как дела")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Привет, мир! Как дела?")


class LLMRewriterLengthRatioGuardTestCase(unittest.TestCase):
    """test_length_ratio_too_short / test_length_ratio_too_long."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def _mock_resp(self, content: str):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": content}}]}
        return mock_resp

    def test_length_ratio_too_short(self):
        # input 100 chars, output 5 chars → ratio 0.05 < 0.35
        long_input = "а" * 100
        short_output = "б" * 5
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp(short_output))
        result = self.rewriter.rewrite(long_input)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_short")

    def test_length_ratio_too_long(self):
        # input 25 chars, output 200 chars → ratio 8.0 > 3.0
        short_input = "а" * 25
        long_output = "б" * 200
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp(long_output))
        result = self.rewriter.rewrite(short_input)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "output_too_long")

    def test_length_ratio_guard_skipped_for_short_input(self):
        # input <= 20 chars — guard не применяется, даже если ratio экстремальный
        short_input = "а" * 20  # ровно 20 — guard не активен
        tiny_output = "б"
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp(tiny_output))
        result = self.rewriter.rewrite(short_input)
        # guard пропущен, ok=True
        self.assertTrue(result.ok)

    def test_length_ratio_within_bounds_passes(self):
        # input 100 chars, output 80 chars → ratio 0.8 — в норме
        normal_input = "а" * 100
        normal_output = "б" * 80
        self.rewriter._session.post = MagicMock(return_value=self._mock_resp(normal_output))
        result = self.rewriter.rewrite(normal_input)
        self.assertTrue(result.ok)


class LLMRewriterTimeoutHandlingTestCase(unittest.TestCase):
    """test_timeout_handling — таймаут обрабатывается как graceful fallback."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=0.1,
            circuit_fail_threshold=3,
        )

    def test_timeout_returns_fallback_no_raise(self):
        import requests as req
        self.rewriter._session.post = MagicMock(side_effect=req.Timeout("timed out"))
        result = self.rewriter.rewrite("какой-то текст для транскрипции")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "timeout")
        self.assertIsNone(result.text)
        self.assertIsNone(result.latency_ms)

    def test_timeout_accumulates_circuit_failures(self):
        import requests as req
        self.rewriter._session.post = MagicMock(side_effect=req.Timeout())
        input_text = "текст один два три четыре пять шесть"
        # 3 таймаута открывают circuit (fail_threshold=3)
        for _ in range(3):
            self.rewriter.rewrite(input_text)
        result = self.rewriter.rewrite(input_text)
        self.assertEqual(result.fallback_reason, "circuit_open")
        # 4-й вызов не должен делать HTTP запрос
        self.assertEqual(self.rewriter._session.post.call_count, 3)


class LLMRewriterWarmupTestCase(unittest.TestCase):
    """Tests for warmup() method and set_model() auto-warmup."""

    def _make_rewriter(self):
        from backend.llm_rewriter import LLMRewriter
        r = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )
        return r

    def _mock_resp(self, status_code=200, text="ok"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        return resp

    def test_warmup_returns_true_on_200(self):
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(200))
        result = r.warmup()
        self.assertTrue(result)

    def test_warmup_returns_false_on_500(self):
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(500))
        result = r.warmup()
        self.assertFalse(result)

    def test_warmup_swallows_exceptions(self):
        import requests as req
        r = self._make_rewriter()
        r._session.post = MagicMock(side_effect=req.ConnectionError("down"))
        # Should not raise
        result = r.warmup()
        self.assertFalse(result)

    def test_warmup_does_not_touch_circuit_breaker(self):
        """warmup failures must NOT count as circuit failures."""
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(500))
        for _ in range(10):
            r.warmup()
        # Circuit should still be CLOSED
        self.assertEqual(r._circuit.state, "closed")

    def test_set_model_fires_warmup_thread(self):
        """set_model with a new model should start a background warmup."""
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(200))
        r.set_model("new-model")
        # Give the daemon thread a short window to run
        import time as _time
        _time.sleep(0.1)
        # warmup should have been called (post was invoked)
        self.assertTrue(r._session.post.called)

    def test_set_model_same_model_no_warmup(self):
        """set_model with the same model should be a no-op (no warmup)."""
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(200))
        r.set_model("test-model")  # same as initial
        import time as _time
        _time.sleep(0.05)
        self.assertFalse(r._session.post.called)

    def test_set_model_resets_circuit_breaker(self):
        """set_model should reset the circuit breaker for the new model."""
        from backend.llm_rewriter import LLMRewriter
        import requests as req
        r = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="model-a",
            circuit_fail_threshold=3,
        )
        r._session.post = MagicMock(side_effect=req.Timeout())
        for _ in range(3):
            r.rewrite("текст")
        self.assertEqual(r._circuit.state, "open")
        # Switch model — circuit should reset
        r.set_model("model-b")
        self.assertEqual(r._circuit.state, "closed")

    def test_warmup_sync_succeeds_on_first_attempt(self):
        """warmup_sync returns immediately when first probe succeeds."""
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(200))
        # Use empty retry_delays so test does not sleep
        r.warmup_sync(timeout_sec=5.0, retry_delays=[])
        # Should have called post exactly once (one warmup probe)
        self.assertEqual(r._session.post.call_count, 1)

    def test_warmup_sync_retries_on_failure_then_succeeds(self):
        """warmup_sync retries with backoff; succeeds on 3rd attempt."""
        r = self._make_rewriter()
        # Fail twice, succeed on third
        fail_resp = self._mock_resp(500)
        ok_resp = self._mock_resp(200)
        r._session.post = MagicMock(side_effect=[fail_resp, fail_resp, ok_resp])
        # Delays of 0 so test does not actually sleep
        r.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0, 0])
        self.assertEqual(r._session.post.call_count, 3)

    def test_warmup_sync_gives_up_after_all_retries(self):
        """warmup_sync logs final warning after exhausting all retry delays."""
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(500))
        # 1 initial + 2 retries = 3 attempts total
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="WARNING") as cm:
            r.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0])
        self.assertEqual(r._session.post.call_count, 3)
        self.assertTrue(
            any("did not succeed after" in line for line in cm.output),
            f"Expected 'did not succeed after' in logs; got: {cm.output}",
        )

    def test_warmup_sync_circuit_not_affected_by_retries(self):
        """Circuit breaker must stay CLOSED after warmup_sync retries fail."""
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(500))
        # 6 total attempts — more than circuit fail_threshold=3
        r.warmup_sync(timeout_sec=5.0, retry_delays=[0, 0, 0, 0, 0])
        self.assertEqual(r._circuit.state, "closed")


class LLMRewriterLoudFailuresTestCase(unittest.TestCase):
    """Tests that timeout/connection/non-200 paths log a warning."""

    def _make_rewriter(self):
        from backend.llm_rewriter import LLMRewriter
        return LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=5.0,
        )

    def _mock_resp(self, status_code=200, body=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = body
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        return resp

    def test_timeout_logs_warning(self):
        import requests as req
        r = self._make_rewriter()
        r._session.post = MagicMock(side_effect=req.Timeout("timed out"))
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="WARNING") as cm:
            r.rewrite("текст один два три")
        self.assertTrue(any("kind=timeout" in line for line in cm.output))

    def test_connection_error_logs_warning(self):
        import requests as req
        r = self._make_rewriter()
        r._session.post = MagicMock(side_effect=req.ConnectionError("refused"))
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="WARNING") as cm:
            r.rewrite("текст один два три")
        self.assertTrue(any("kind=connection_error" in line for line in cm.output))

    def test_non_200_logs_warning(self):
        r = self._make_rewriter()
        r._session.post = MagicMock(return_value=self._mock_resp(500, "Internal Error"))
        with self.assertLogs("KrabEar.Backend.LLMRewriter", level="WARNING") as cm:
            r.rewrite("текст один два три")
        self.assertTrue(any("kind=http_error" in line for line in cm.output))
        self.assertTrue(any("status=500" in line for line in cm.output))


class LLMRewriter503JitRetryTestCase(unittest.TestCase):
    """Tests for the 503 JIT retry path (section 5 of _rewrite_impl)."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=5.0,
        )

    def _resp(self, status_code, content=None):
        mock = MagicMock()
        mock.status_code = status_code
        mock.text = ""
        if content is not None:
            mock.json.return_value = {"choices": [{"message": {"content": content}}]}
        return mock

    @patch("backend.llm_rewriter.time.sleep")
    def test_jit_retry_503_succeeds(self, mock_sleep):
        rewritten = "Исправленный текст готов."
        self.rewriter._session.post = MagicMock(
            side_effect=[self._resp(503), self._resp(200, rewritten)]
        )
        result = self.rewriter.rewrite("исходный текст для проверки retry")
        self.assertTrue(result.ok)
        self.assertEqual(result.text_or_fallback("raw"), rewritten)
        self.assertIsNotNone(result.latency_ms)
        # Use assertIn instead of assert_called_once_with to guard against spurious
        # time.sleep() calls from background threads of unrelated BackendService
        # instances in the same xdist worker (patch targets the shared time module).
        from unittest.mock import call as _call
        self.assertIn(_call(10), mock_sleep.call_args_list,
                      "Expected time.sleep(10) to be called by 503 JIT retry path")
        self.assertEqual(self.rewriter._circuit.state, "closed")

    @patch("backend.llm_rewriter.time.sleep")
    def test_jit_retry_503_then_503_fails(self, mock_sleep):
        self.rewriter._session.post = MagicMock(
            side_effect=[self._resp(503), self._resp(503)]
        )
        result = self.rewriter.rewrite("исходный текст для проверки retry")
        self.assertFalse(result.ok)
        self.assertTrue(result.fallback_reason.startswith("http_503"))
        self.assertEqual(self.rewriter._circuit._consecutive_failures, 1)

    @patch("backend.llm_rewriter.time.sleep")
    def test_jit_retry_503_no_recursion(self, mock_sleep):
        self.rewriter._session.post = MagicMock(
            side_effect=[self._resp(503), self._resp(503)]
        )
        self.rewriter.rewrite("исходный текст для проверки retry")
        self.assertEqual(self.rewriter._session.post.call_count, 2)


class LLMRewriterErrorBusPushTests(unittest.TestCase):
    """Verify error_bus.push is called at all 6 failure paths in _rewrite_impl."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.error_bus = MagicMock()
        self.rewriter = LLMRewriter(
            base_url="http://127.0.0.1:1234/v1",
            api_key="x",
            model="gemma-4-e4b-it-mlx",
            timeout_sec=0.01,
        )
        self.rewriter._error_bus = self.error_bus  # late injection per Task 7

    def _pushed_codes(self):
        return [c.args[0].code for c in self.error_bus.push.call_args_list]

    def test_timeout_pushes_rewriter_timeout(self):
        with patch.object(self.rewriter._session, "post", side_effect=requests.Timeout()):
            self.rewriter.rewrite("text")
        self.assertIn("rewriter.timeout", self._pushed_codes())

    def test_connection_error_pushes_rewriter_connection_error(self):
        with patch.object(self.rewriter._session, "post",
                          side_effect=requests.ConnectionError("refused")):
            self.rewriter.rewrite("text")
        self.assertIn("rewriter.connection_error", self._pushed_codes())

    def test_http_500_pushes_rewriter_error(self):
        # After non-200 response — should push a rewriter.* code
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "internal error"
        with patch.object(self.rewriter._session, "post", return_value=resp):
            self.rewriter.rewrite("text")
        codes = self._pushed_codes()
        self.assertTrue(any(c.startswith("rewriter.") for c in codes))

    def test_tool_calls_only_pushes_rewriter_tool_calls_emitted(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": "", "tool_calls": [{"id": "1"}]}}]
        }
        with patch.object(self.rewriter._session, "post", return_value=resp):
            self.rewriter.rewrite("text")
        self.assertIn("rewriter.tool_calls_emitted", self._pushed_codes())

    def test_empty_content_pushes_rewriter_empty_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": ""}}]}
        with patch.object(self.rewriter._session, "post", return_value=resp):
            self.rewriter.rewrite("text")
        self.assertIn("rewriter.empty_response", self._pushed_codes())

    def test_parse_error_pushes_rewriter_parse_error(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        with patch.object(self.rewriter._session, "post", return_value=resp):
            self.rewriter.rewrite("text")
        self.assertIn("rewriter.parse_error", self._pushed_codes())

    def test_force_timeout_env_simulates_timeout(self):
        os.environ["KRAB_EAR_LLM_FORCE_TIMEOUT"] = "1"
        try:
            result = self.rewriter.rewrite("text")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "timeout")
            self.assertIn("rewriter.timeout", self._pushed_codes())
        finally:
            os.environ.pop("KRAB_EAR_LLM_FORCE_TIMEOUT", None)


class LLMRewriterPassiveHealthCheckTestCase(unittest.TestCase):
    """Tests for passive_health_check() — GET /v1/models, no JIT reload."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="gemma-4-e4b-it-mlx",
        )

    def _mock_get_response(self, data: dict, status_code: int = 200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data
        return resp

    def test_passive_health_check_returns_true_when_model_loaded(self):
        """GET /v1/models returns our model in data list → (True, True)."""
        resp = self._mock_get_response({"data": [{"id": "gemma-4-e4b-it-mlx"}]})
        with patch.object(self.rewriter._session, "get", return_value=resp) as mock_get:
            result = self.rewriter.passive_health_check()
        self.assertEqual(result, (True, True))
        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        self.assertIn("/models", call_url)
        self.assertNotIn("chat", call_url)  # must NOT be a chat/completions call

    def test_passive_health_check_returns_false_when_model_not_loaded(self):
        """GET /v1/models returns a different model → (True, False) — reachable but our
        model is not in the loaded list."""
        resp = self._mock_get_response({"data": [{"id": "llama-3-8b"}]})
        with patch.object(self.rewriter._session, "get", return_value=resp):
            result = self.rewriter.passive_health_check()
        self.assertEqual(result, (True, False))

    def test_passive_health_check_returns_false_when_unreachable(self):
        """ConnectionError from session.get → (False, False) — never raises."""
        import requests as req
        with patch.object(
            self.rewriter._session, "get", side_effect=req.ConnectionError("refused")
        ):
            result = self.rewriter.passive_health_check()
        self.assertEqual(result, (False, False))


if __name__ == "__main__":
    unittest.main()
