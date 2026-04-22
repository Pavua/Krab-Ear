"""Тесты для LLMRewriteStage."""

from backend.llm_rewriter import LLMRewriteResult
from core.pipeline.stages.llm_rewrite import LLMRewriteStage
from core.pipeline.context import PipelineContext
import sys
import os
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_ctx(cleaned_text="Привет мир", raw_text=""):
    ctx = PipelineContext(audio_input=None)
    ctx.cleaned_text = cleaned_text
    ctx.raw_text = raw_text
    return ctx


def _mock_rewriter(ok=True, text="Исправленный текст", fallback=None, latency=42):
    rewriter = MagicMock()
    rewriter.rewrite.return_value = LLMRewriteResult(
        ok=ok, text=text, fallback_reason=fallback, latency_ms=latency
    )
    circuit = MagicMock()
    circuit.state = "closed"
    rewriter._circuit = circuit
    return rewriter


class TestLLMRewriteStageInit(unittest.TestCase):
    def test_name_is_llm_rewrite(self):
        stage = LLMRewriteStage(rewriter=None)
        self.assertEqual(stage.name, "llm_rewrite")

    def test_should_run_false_when_no_rewriter(self):
        stage = LLMRewriteStage(rewriter=None, settings_get=lambda k, d=None: True)
        ctx = _make_ctx()
        self.assertFalse(stage.should_run(ctx))


class TestLLMRewriteStageShouldRun(unittest.TestCase):
    def test_should_run_false_when_disabled_in_settings(self):
        rewriter = _mock_rewriter()
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: False)
        ctx = _make_ctx()
        self.assertFalse(stage.should_run(ctx))

    def test_should_run_false_when_circuit_open(self):
        rewriter = _mock_rewriter()
        rewriter._circuit.state = "open"
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx()
        self.assertFalse(stage.should_run(ctx))

    def test_should_run_true_when_all_conditions_met(self):
        rewriter = _mock_rewriter()
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx()
        self.assertTrue(stage.should_run(ctx))

    def test_should_run_true_when_circuit_half_open(self):
        rewriter = _mock_rewriter()
        rewriter._circuit.state = "half_open"
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx()
        self.assertTrue(stage.should_run(ctx))


class TestLLMRewriteStageProcess(unittest.TestCase):
    def test_process_ok_sets_rewritten_text_and_final_text(self):
        rewriter = _mock_rewriter(ok=True, text="Исправленный текст", latency=55)
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="привет мир")
        result = stage.process(ctx)
        self.assertTrue(result.llm_applied)
        self.assertEqual(result.rewritten_text, "Исправленный текст")
        self.assertEqual(result.final_text, "Исправленный текст")
        self.assertEqual(result.llm_latency_ms, 55)

    def test_process_ok_false_keeps_original_in_rewritten(self):
        rewriter = _mock_rewriter(ok=False, text=None, fallback="circuit_open", latency=None)
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="оригинал")
        result = stage.process(ctx)
        self.assertFalse(result.llm_applied)
        self.assertEqual(result.rewritten_text, "оригинал")
        self.assertEqual(result.llm_fallback_reason, "circuit_open")

    def test_process_uses_cleaned_text_over_raw(self):
        rewriter = _mock_rewriter(ok=True, text="результат")
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="cleaned", raw_text="raw")
        stage.process(ctx)
        rewriter.rewrite.assert_called_once_with("cleaned")

    def test_process_falls_back_to_raw_when_cleaned_empty(self):
        rewriter = _mock_rewriter(ok=True, text="из raw")
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="", raw_text="raw текст")
        stage.process(ctx)
        rewriter.rewrite.assert_called_once_with("raw текст")

    def test_process_handles_unexpected_exception_gracefully(self):
        rewriter = MagicMock()
        rewriter.rewrite.side_effect = RuntimeError("unexpected!")
        circuit = MagicMock()
        circuit.state = "closed"
        rewriter._circuit = circuit
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="текст")
        result = stage.process(ctx)
        self.assertFalse(result.llm_applied)
        self.assertEqual(result.rewritten_text, "текст")
        self.assertTrue(any("llm_rewrite_unexpected" in e for e in result.errors))

    def test_process_does_not_raise(self):
        """process() НИКОГДА не raises — contract проверка."""
        rewriter = MagicMock()
        rewriter.rewrite.side_effect = Exception("boom")
        rewriter._circuit = MagicMock(state="closed")
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx()
        try:
            stage.process(ctx)
        except Exception as e:
            self.fail(f"process() raised unexpectedly: {e}")

    def test_process_latency_recorded_on_failure(self):
        rewriter = _mock_rewriter(ok=False, text=None, fallback="timeout", latency=100)
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx()
        result = stage.process(ctx)
        self.assertEqual(result.llm_latency_ms, 100)

    def test_default_settings_get_disables_stage(self):
        """Без settings_get llm_rewrite_enabled дефолтно False."""
        rewriter = _mock_rewriter()
        stage = LLMRewriteStage(rewriter=rewriter)
        ctx = _make_ctx()
        self.assertFalse(stage.should_run(ctx))


if __name__ == "__main__":
    unittest.main()


class TestLLMRewriteEdgeCases(unittest.TestCase):
    """Edge cases: circuit breaker states, max tokens, empty inputs."""

    def test_circuit_breaker_open_skips_rewrite(self):
        rewriter = _mock_rewriter()
        rewriter._circuit.state = "open"
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="текст")
        stage.process(ctx)
        # Circuit open → should_run returns False
        self.assertFalse(stage.should_run(ctx))

    def test_circuit_breaker_half_open_allows_retry(self):
        rewriter = _mock_rewriter()
        rewriter._circuit.state = "half_open"
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="текст")
        # should_run True in half_open
        self.assertTrue(stage.should_run(ctx))

    def test_max_tokens_exceeded(self):
        # Simulate rewriter hitting max tokens limit
        rewriter = _mock_rewriter(ok=False, fallback="max_tokens_exceeded", latency=100)
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="очень очень очень очень очень очень длинный текст " * 100)
        result = stage.process(ctx)
        self.assertFalse(result.llm_applied)
        self.assertEqual(result.llm_fallback_reason, "max_tokens_exceeded")

    def test_empty_cleaned_text_fallback_to_raw(self):
        rewriter = _mock_rewriter(ok=True, text="из raw")
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="", raw_text="raw текст для переписи")
        stage.process(ctx)
        # Should call rewriter with raw_text
        rewriter.rewrite.assert_called_with("raw текст для переписи")

    def test_both_cleaned_and_raw_empty(self):
        rewriter = _mock_rewriter(ok=True, text="default")
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="", raw_text="")
        stage.process(ctx)
        # Should still call with empty string
        rewriter.rewrite.assert_called_once_with("")

    def test_timeout_during_rewrite(self):
        rewriter = _mock_rewriter(ok=False, fallback="timeout", latency=5000)
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="текст")
        result = stage.process(ctx)
        self.assertFalse(result.llm_applied)
        self.assertEqual(result.llm_fallback_reason, "timeout")
        self.assertEqual(result.llm_latency_ms, 5000)

    def test_very_short_input_text(self):
        rewriter = _mock_rewriter(ok=True, text="a")
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="a")
        result = stage.process(ctx)
        self.assertTrue(result.llm_applied)

    def test_rewriter_returns_none_text(self):
        # Rewriter returns ok=True but text=None (edge case)
        rewriter = _mock_rewriter(ok=True, text=None)
        stage = LLMRewriteStage(rewriter=rewriter, settings_get=lambda k, d=None: True)
        ctx = _make_ctx(cleaned_text="оригинал")
        result = stage.process(ctx)
        # Should handle gracefully
        self.assertIsNotNone(result.rewritten_text)

    def test_settings_get_missing_returns_default(self):
        # settings_get not provided → defaults to False
        rewriter = _mock_rewriter()
        stage = LLMRewriteStage(rewriter=rewriter)
        ctx = _make_ctx(cleaned_text="текст")
        # should_run returns False (default disabled)
        self.assertFalse(stage.should_run(ctx))
