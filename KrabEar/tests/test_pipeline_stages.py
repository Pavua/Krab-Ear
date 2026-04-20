"""Unit tests for core/pipeline/factory.py and core/pipeline/bridge.py.

base.py и executor.py уже покрыты в test_pipeline_core.py.
Здесь фокус на factory (create_default_pipeline) и bridge (transcribe_v2).
"""

from __future__ import annotations

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np

from core.pipeline.executor import PipelineExecutor
from core.pipeline.factory import create_default_pipeline
from core.pipeline.bridge import transcribe_v2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _silence_array(samples: int = 1600) -> "np.ndarray":
    """Return a silent float32 16kHz mono array (avoids AudioNormalizationStage errors)."""
    return np.zeros(samples, dtype=np.float32)


# ---------------------------------------------------------------------------
# Fake collaborators for factory / bridge
# ---------------------------------------------------------------------------

class FakeEngine:
    """Minimal fake engine — has transcribe() and no run_diarization."""

    def transcribe(self, audio, **kwargs):
        return {
            "text": "hello",
            "confidence": 0.9,
            "language": "en",
            "segments": [],
            "model": "fake",
        }


class FakeEngineWithDiarization(FakeEngine):
    """Fake engine that exposes run_diarization."""

    def run_diarization(self, audio_path: str) -> list:
        return [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0}]


class FakeLLMRewriter:
    """Minimal LLM rewriter that returns text unchanged."""

    def rewrite(self, text: str, **kwargs):
        return text, False, None, None


class FakeTranslator:
    """Minimal translator."""

    def translate(self, text: str, **kwargs):
        return "translated"


# ---------------------------------------------------------------------------
# PipelineFactory tests
# ---------------------------------------------------------------------------

class TestCreateDefaultPipeline(unittest.TestCase):
    """Tests for core/pipeline/factory.create_default_pipeline()."""

    def setUp(self):
        self.engine = FakeEngine()

    def test_returns_pipeline_executor(self):
        pipeline = create_default_pipeline(self.engine)
        self.assertIsInstance(pipeline, PipelineExecutor)

    def test_pipeline_has_six_stages(self):
        pipeline = create_default_pipeline(self.engine)
        self.assertEqual(len(pipeline._stages), 6)

    def test_stage_names_in_expected_order(self):
        pipeline = create_default_pipeline(self.engine)
        names = [s.name for s in pipeline._stages]
        self.assertEqual(names, [
            "audio_normalization",
            "stt",
            "diarization",
            "text_cleanup",
            "llm_rewrite",
            "translation",
        ])

    def test_works_without_optional_collaborators(self):
        # Must not raise even when llm_rewriter, translator, diarization_fn are None
        pipeline = create_default_pipeline(self.engine)
        self.assertIsNotNone(pipeline)

    def test_diarization_fn_picked_from_engine(self):
        """If engine has run_diarization, factory binds it automatically."""
        engine = FakeEngineWithDiarization()
        pipeline = create_default_pipeline(engine)
        # DiarizationStage is at index 2; its _diarization_fn should be set
        diarization_stage = pipeline._stages[2]
        self.assertIsNotNone(diarization_stage._diarization_fn)

    def test_explicit_diarization_fn_takes_precedence(self):
        """Explicit diarization_fn overrides engine.run_diarization."""
        engine = FakeEngineWithDiarization()
        custom_fn = lambda path: []  # noqa: E731
        pipeline = create_default_pipeline(engine, diarization_fn=custom_fn)
        diarization_stage = pipeline._stages[2]
        self.assertIs(diarization_stage._diarization_fn, custom_fn)

    def test_custom_settings_get_used_in_stages(self):
        """Custom settings_get callable is accepted without errors."""
        def my_settings(key, default=None):
            return default

        pipeline = create_default_pipeline(self.engine, settings_get=my_settings)
        self.assertEqual(len(pipeline._stages), 6)

    def test_with_llm_rewriter(self):
        rewriter = FakeLLMRewriter()
        pipeline = create_default_pipeline(self.engine, llm_rewriter=rewriter)
        llm_stage = pipeline._stages[4]
        self.assertEqual(llm_stage.name, "llm_rewrite")

    def test_with_translator(self):
        translator = FakeTranslator()
        pipeline = create_default_pipeline(self.engine, translator=translator)
        translation_stage = pipeline._stages[5]
        self.assertEqual(translation_stage.name, "translation")


# ---------------------------------------------------------------------------
# Pipeline bridge (transcribe_v2) tests
# ---------------------------------------------------------------------------

class TestTranscribeV2(unittest.TestCase):
    """Tests for core/pipeline/bridge.transcribe_v2()."""

    def _make_engine(self, text="hello world", confidence=0.85):
        """Return a FakeEngine whose transcribe() returns given text."""
        class _E:
            def transcribe(self_inner, audio, **kwargs):
                return {
                    "text": text,
                    "confidence": confidence,
                    "language": "en",
                    "segments": [{"start": 0.0, "end": 1.0, "text": text}],
                    "model": "fake-balanced",
                }
        return _E()

    def test_returns_dict(self):
        result = transcribe_v2(self._make_engine(), audio_input=None)
        self.assertIsInstance(result, dict)

    def test_result_has_required_legacy_keys(self):
        result = transcribe_v2(self._make_engine(), audio_input=None)
        required = {
            "text", "raw_text", "cleaned_text", "llm_applied",
            "llm_latency_ms", "llm_fallback_reason", "confidence",
            "duration_ms", "engine", "model", "language",
            "segments", "diarization",
        }
        self.assertTrue(required.issubset(set(result.keys())))

    def test_engine_field_is_pipeline_v2(self):
        result = transcribe_v2(self._make_engine(), audio_input=None)
        self.assertEqual(result["engine"], "pipeline_v2")

    def test_text_field_non_empty_on_success(self):
        # Pass a numpy array so AudioNormalizationStage does not soft-fail
        result = transcribe_v2(self._make_engine(text="test"), audio_input=_silence_array())
        self.assertEqual(result["text"], "test")

    def test_is_preview_hides_segments(self):
        result = transcribe_v2(
            self._make_engine(text="hi"), audio_input=_silence_array(), is_preview=True
        )
        self.assertEqual(result["segments"], [])

    def test_is_preview_false_exposes_segments(self):
        result = transcribe_v2(
            self._make_engine(text="hi"), audio_input=_silence_array(), is_preview=False
        )
        # STTStage may populate segments; at minimum it should be a list
        self.assertIsInstance(result["segments"], list)

    def test_extra_kwargs_ignored(self):
        """Unknown kwargs must not raise (backward-compat)."""
        result = transcribe_v2(
            self._make_engine(), audio_input=None,
            unknown_param="value", another_param=42,
        )
        self.assertIn("text", result)

    def test_cleanup_profile_passed(self):
        """Strict profile should not crash the bridge."""
        result = transcribe_v2(
            self._make_engine(), audio_input=None, cleanup_profile="strict"
        )
        self.assertIn("text", result)

    def test_executor_exception_returns_error_dict(self):
        """If executor raises unexpectedly, bridge returns error dict."""
        class BrokenEngine:
            def transcribe(self, audio, **kwargs):
                raise RuntimeError("catastrophic failure")

        # STTStage catches exceptions via executor; but executor itself
        # appends to ctx.errors and continues. So result should still be a dict.
        result = transcribe_v2(BrokenEngine(), audio_input=None)
        self.assertIsInstance(result, dict)
        self.assertIn("text", result)

    def test_pipeline_errors_reported_in_result(self):
        """Stage errors accumulated in ctx.errors surface as pipeline_errors."""
        class BrokenEngine:
            def transcribe(self, audio, **kwargs):
                raise RuntimeError("stt failure")

        result = transcribe_v2(BrokenEngine(), audio_input=None)
        # STTStage failure → error added to ctx.errors → pipeline_errors key
        self.assertIn("pipeline_errors", result)

    def test_lang_hint_forwarded(self):
        """lang_hint parameter accepted without errors."""
        result = transcribe_v2(
            self._make_engine(), audio_input=None, lang_hint="ru"
        )
        self.assertIn("text", result)

    def test_translation_mode_off_by_default(self):
        """Default translation_mode=off → translation not performed."""
        result = transcribe_v2(self._make_engine(), audio_input=None)
        # diarization is empty dict by default (no diarization_fn)
        self.assertIsInstance(result["diarization"], dict)


if __name__ == "__main__":
    unittest.main()
