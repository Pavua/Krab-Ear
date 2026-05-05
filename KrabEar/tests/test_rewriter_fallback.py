"""Tests for RewriterFallbackChain.

All tests mock _call_model — no real HTTP requests.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.llm_rewriter import (
    CircuitBreaker,
    FallbackRewriteResult,
    LLMRewriteResult,
    LLMRewriter,
    RewriterFallbackChain,
)


def _make_rewriter(model="primary-model"):
    rw = LLMRewriter.__new__(LLMRewriter)
    rw._base_url = "http://localhost:1234/v1"
    rw._api_key = ""
    rw._model = model
    rw._timeout = 5.0
    rw._circuit = CircuitBreaker(fail_threshold=3, initial_reset_sec=60, max_reset_sec=600)
    rw._last_latency_ms = None
    rw._last_error = None
    rw._session = MagicMock()
    rw._error_bus = None
    return rw


def _ok_result(text="Rewritten"):
    return LLMRewriteResult(ok=True, text=text, fallback_reason=None, latency_ms=10)


def _fail_result(reason="timeout"):
    return LLMRewriteResult(ok=False, text=None, fallback_reason=reason, latency_ms=None)


def _open_breaker():
    cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=9999, max_reset_sec=9999)
    cb.record_failure()
    assert cb.state == "open"
    return cb


class TestPrimarySucceeds(unittest.TestCase):
    def test_primary_succeeds_returns_primary_result(self):
        rw = _make_rewriter("primary-model")
        chain = RewriterFallbackChain(rw, ["fallback-a", "fallback-b"])
        with patch.object(rw, "rewrite", return_value=_ok_result("Fixed")) as mock_rw:
            result = chain.rewrite("Hello")
        mock_rw.assert_called_once_with("Hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Fixed")
        self.assertEqual(result.model_used, "primary-model")
        self.assertFalse(result.fallback_used)

    def test_primary_succeeds_no_fallback_breakers_touched(self):
        rw = _make_rewriter("primary-model")
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", return_value=_ok_result()):
            chain.rewrite("test")
        self.assertEqual(chain._fallback_breakers["fallback-a"].state, "closed")


class TestPrimaryFails(unittest.TestCase):
    def test_primary_fails_falls_back_to_secondary(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", side_effect=[_fail_result(), _ok_result("From fallback")]):
            result = chain.rewrite("Hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "From fallback")
        self.assertEqual(result.model_used, "fallback-a")
        self.assertTrue(result.fallback_used)

    def test_primary_fails_tries_fallbacks_in_order(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a", "fallback-b"])
        with patch.object(rw, "rewrite", side_effect=[
            _fail_result(), _fail_result(), _ok_result("From b")
        ]):
            result = chain.rewrite("Hi")
        self.assertTrue(result.ok)
        self.assertEqual(result.model_used, "fallback-b")

    def test_all_fail_returns_raw_text(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a", "fallback-b"])
        with patch.object(rw, "rewrite", return_value=_fail_result("connection_error")):
            result = chain.rewrite("Hi")
        self.assertFalse(result.ok)
        self.assertIsNone(result.text)
        self.assertIsNone(result.model_used)
        self.assertIn("all_models_failed", result.fallback_reason)

    def test_empty_chain_returns_raw_immediately(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, [])
        with patch.object(rw, "rewrite", return_value=_fail_result("timeout")):
            result = chain.rewrite("Hi")
        self.assertFalse(result.ok)
        self.assertIn("all_models_failed", result.fallback_reason)


class TestCircuitBreakers(unittest.TestCase):
    def test_open_breaker_skipped_in_chain(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a", "fallback-b"])
        chain._fallback_breakers["fallback-a"] = _open_breaker()
        with patch.object(rw, "rewrite", side_effect=[_fail_result(), _ok_result("From b")]):
            result = chain.rewrite("Hi")
        self.assertTrue(result.ok)
        self.assertEqual(result.model_used, "fallback-b")

    def test_all_breakers_open_returns_raw(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        rw._circuit = _open_breaker()
        chain._fallback_breakers["fallback-a"] = _open_breaker()
        with patch.object(rw, "rewrite", return_value=_fail_result("circuit_open")):
            result = chain.rewrite("Hi")
        self.assertFalse(result.ok)
        self.assertIn("all_models_failed", result.fallback_reason)


class TestFallbackUsedFlag(unittest.TestCase):
    def test_fallback_used_flag_true_when_secondary_used(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", side_effect=[_fail_result(), _ok_result()]):
            result = chain.rewrite("Hi")
        self.assertTrue(result.fallback_used)

    def test_fallback_used_flag_false_when_primary_used(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", return_value=_ok_result()):
            result = chain.rewrite("Hi")
        self.assertFalse(result.fallback_used)

    def test_fallback_used_flag_false_when_all_fail(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", return_value=_fail_result()):
            result = chain.rewrite("Hi")
        self.assertFalse(result.fallback_used)


class TestErrorBus(unittest.TestCase):
    def test_fallback_used_pushes_error_bus(self):
        rw = _make_rewriter()
        mock_bus = MagicMock()
        rw._error_bus = mock_bus
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", side_effect=[_fail_result(), _ok_result()]):
            result = chain.rewrite("Hi")
        self.assertTrue(result.ok)
        mock_bus.push.assert_called_once()
        pushed = mock_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "rewriter.fallback_used")
        self.assertEqual(pushed.severity, "info")

    def test_primary_success_does_not_push_error_bus(self):
        rw = _make_rewriter()
        mock_bus = MagicMock()
        rw._error_bus = mock_bus
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", return_value=_ok_result()):
            chain.rewrite("Hi")
        mock_bus.push.assert_not_called()

    def test_no_error_bus_fallback_still_works(self):
        rw = _make_rewriter()
        rw._error_bus = None
        chain = RewriterFallbackChain(rw, ["fallback-a"])
        with patch.object(rw, "rewrite", side_effect=[_fail_result(), _ok_result()]):
            result = chain.rewrite("Hi")
        self.assertTrue(result.ok)


class TestSettingsOrder(unittest.TestCase):
    def test_chain_respects_settings_order(self):
        rw = _make_rewriter("primary-model")
        chain = RewriterFallbackChain(rw, ["model-first", "model-second"])
        used_models = []

        def fake_rewrite(text):
            used_models.append(rw._model)
            if rw._model == "primary-model":
                return _fail_result()
            if rw._model == "model-first":
                return _ok_result("from first")
            return _ok_result("from second")

        with patch.object(rw, "rewrite", side_effect=fake_rewrite):
            result = chain.rewrite("Hi")

        self.assertEqual(result.model_used, "model-first")
        self.assertNotIn("model-second", used_models)


class TestHelpers(unittest.TestCase):
    def test_text_or_fallback_returns_text_when_ok(self):
        result = FallbackRewriteResult(
            ok=True, text="Rewritten", model_used="m", fallback_used=False,
            fallback_reason=None, latency_ms=5
        )
        self.assertEqual(result.text_or_fallback("raw"), "Rewritten")

    def test_text_or_fallback_returns_fallback_when_not_ok(self):
        result = FallbackRewriteResult(
            ok=False, text=None, model_used=None, fallback_used=False,
            fallback_reason="all_models_failed:timeout", latency_ms=None
        )
        self.assertEqual(result.text_or_fallback("raw text"), "raw text")


class TestStatus(unittest.TestCase):
    def test_status_contains_primary_and_fallbacks(self):
        rw = _make_rewriter()
        chain = RewriterFallbackChain(rw, ["fallback-a", "fallback-b"])
        st = chain.status()
        self.assertIn("primary", st)
        self.assertIn("fallback_models", st)
        self.assertIn("fallback_breakers", st)
        self.assertEqual(st["fallback_models"], ["fallback-a", "fallback-b"])


if __name__ == "__main__":
    unittest.main()
