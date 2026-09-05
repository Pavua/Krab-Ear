"""Unit tests для метода LLMRewriter.summarize() — не покрыт в test_llm_rewriter.py."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriter

_LOADED_PROBE = None


def setUpModule():
    """C1: summarize probes Studio catalog. Unknown (None) → старый HTTP-путь."""
    global _LOADED_PROBE
    _LOADED_PROBE = patch(
        "backend.lm_studio_lifecycle.probe_loaded_chat_models",
        return_value=None,
    )
    _LOADED_PROBE.start()


def tearDownModule():
    _LOADED_PROBE.stop()


def _mock_ok_response(content: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def _mock_error_response(status_code: int):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "Error"
    return resp


class SummarizeEmptyInputTestCase(unittest.TestCase):
    """Пустой/whitespace ввод → empty_input без HTTP вызова."""

    def setUp(self):
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_empty_string_returns_empty_input(self):
        with patch.object(self.rewriter._session, "post") as mock_post:
            result = self.rewriter.summarize("")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "empty_input")
            mock_post.assert_not_called()

    def test_whitespace_only_returns_empty_input(self):
        with patch.object(self.rewriter._session, "post") as mock_post:
            result = self.rewriter.summarize("   \n  \t")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "empty_input")
            mock_post.assert_not_called()


class SummarizeSuccessTestCase(unittest.TestCase):
    """Happy path: успешный summary."""

    def setUp(self):
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_successful_summary_returns_ok(self):
        with patch.object(self.rewriter._session, "post",
                          return_value=_mock_ok_response("Краткое резюме текста.")):
            result = self.rewriter.summarize("Длинный текст для резюмирования.")
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "Краткое резюме текста.")
            self.assertIsNone(result.fallback_reason)

    def test_summary_has_latency_ms(self):
        with patch.object(self.rewriter._session, "post",
                          return_value=_mock_ok_response("Summary.")):
            result = self.rewriter.summarize("Some text to summarize.")
            self.assertIsNotNone(result.latency_ms)
            self.assertGreaterEqual(result.latency_ms, 0)

    def test_summary_uses_post_to_chat_completions(self):
        with patch.object(self.rewriter._session, "post",
                          return_value=_mock_ok_response("Summary.")) as mock_post:
            self.rewriter.summarize("Some text.")
            args, _ = mock_post.call_args
            self.assertEqual(args[0], "http://localhost:1234/v1/chat/completions")

    def test_summary_sends_authorization_header(self):
        with patch.object(self.rewriter._session, "post",
                          return_value=_mock_ok_response("Summary.")) as mock_post:
            self.rewriter.summarize("Text.")
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_summary_uses_max_sentences_in_system_prompt(self):
        with patch.object(self.rewriter._session, "post",
                          return_value=_mock_ok_response("Summary.")) as mock_post:
            self.rewriter.summarize("Text.", max_sentences=5)
            _, kwargs = mock_post.call_args
            messages = kwargs["json"]["messages"]
            system_content = messages[0]["content"]
            self.assertIn("5", system_content)

    def test_summary_default_max_sentences_3(self):
        with patch.object(self.rewriter._session, "post",
                          return_value=_mock_ok_response("Summary.")) as mock_post:
            self.rewriter.summarize("Text.")
            _, kwargs = mock_post.call_args
            messages = kwargs["json"]["messages"]
            system_content = messages[0]["content"]
            self.assertIn("3", system_content)

    def test_summary_timeout_is_doubled(self):
        """summarize() использует timeout * 2 (summary может быть длиннее)."""
        rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=5.0,
        )
        with patch.object(rewriter._session, "post",
                          return_value=_mock_ok_response("Summary.")) as mock_post:
            rewriter.summarize("Text.")
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs["timeout"], 10.0)


class SummarizeFailureTestCase(unittest.TestCase):
    """Failure modes: timeout, connection error, HTTP errors, parse errors, empty response."""

    def setUp(self):
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_timeout_returns_fallback(self):
        import requests as req
        with patch.object(self.rewriter._session, "post",
                          side_effect=req.Timeout("timeout")):
            result = self.rewriter.summarize("Some text here.")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "timeout")

    def test_connection_error_returns_fallback(self):
        import requests as req
        with patch.object(self.rewriter._session, "post",
                          side_effect=req.ConnectionError("refused")):
            result = self.rewriter.summarize("Some text here.")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "connection_error")

    def test_http_500_returns_fallback(self):
        with patch.object(self.rewriter._session, "post",
                          return_value=_mock_error_response(500)):
            result = self.rewriter.summarize("Some text here.")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "http_500")

    def test_malformed_json_returns_parse_error(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        with patch.object(self.rewriter._session, "post", return_value=resp):
            result = self.rewriter.summarize("Some text here.")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "parse_error")

    def test_empty_content_returns_empty_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": ""}}]}
        with patch.object(self.rewriter._session, "post", return_value=resp):
            result = self.rewriter.summarize("Some text here.")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "empty_response")

    def test_missing_choices_returns_parse_error(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"error": "no model"}
        with patch.object(self.rewriter._session, "post", return_value=resp):
            result = self.rewriter.summarize("Some text here.")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "parse_error")


class SummarizeCircuitBreakerTestCase(unittest.TestCase):
    """Circuit breaker интеграция с summarize()."""

    def setUp(self):
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            circuit_fail_threshold=3,
        )

    def test_circuit_opens_after_3_failures(self):
        import requests as req
        with patch.object(self.rewriter._session, "post",
                          side_effect=req.ConnectionError()) as mock_post:
            for _ in range(3):
                self.rewriter.summarize("text")
            result = self.rewriter.summarize("text")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "circuit_open")
            self.assertEqual(mock_post.call_count, 3)

    def test_circuit_open_blocks_summarize(self):
        """После открытия circuit summarize возвращает circuit_open."""
        import requests as req
        with patch.object(self.rewriter._session, "post",
                          side_effect=req.ConnectionError()):
            # Открываем circuit через rewrite
            for _ in range(3):
                self.rewriter.rewrite("text to rewrite")
            # summarize тоже должен получить circuit_open
            result = self.rewriter.summarize("some summary text")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "circuit_open")


if __name__ == "__main__":
    unittest.main()
