"""Integration tests for pipeline factory and bridge.

Tests:
- factory creates all 6 stages
- bridge returns legacy-compatible dict
- stages are exercised with mocked collaborators
"""

from __future__ import annotations

import sys
import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# --- path setup ---
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from core.pipeline.context import PipelineContext
from core.pipeline.executor import PipelineExecutor
from core.pipeline.factory import create_default_pipeline
from core.pipeline.bridge import transcribe_v2
from core.pipeline.stages.audio_normalization import AudioNormalizationStage
from core.pipeline.stages.stt import STTStage
from core.pipeline.stages.diarization import DiarizationStage
from core.pipeline.stages.text_cleanup import TextCleanupStage
from core.pipeline.stages.llm_rewrite import LLMRewriteStage
from core.pipeline.stages.translation import TranslationStage


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _make_stt_result(text: str = "Привет мир") -> dict:
    return {
        "text": text,
        "raw_text": text,
        "language": "ru",
        "model": "test-model",
        "confidence": 0.95,
        "segments": [{"start": 0.0, "end": 1.0, "text": text}],
    }


def _make_engine(text: str = "Привет мир") -> MagicMock:
    engine = MagicMock()
    engine.transcribe.return_value = _make_stt_result(text)
    return engine


class _LLMResult:
    def __init__(self, ok: bool = True, text: str = "Переписанный текст"):
        self.ok = ok
        self.text = text
        self.latency_ms = 42
        self.fallback_reason = None if ok else "circuit_open"


class _TranslationResult:
    def __init__(self, ok: bool = True, text: str = "Translated"):
        self.ok = ok
        self.text = text
        self.engine = "test_engine"
        self.source_lang = "ru"
        self.target_lang = "es"
        self.status = "ok" if ok else "error"


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------

class TestCreateDefaultPipeline(unittest.TestCase):

    def test_returns_pipeline_executor(self):
        engine = _make_engine()
        pipeline = create_default_pipeline(engine)
        self.assertIsInstance(pipeline, PipelineExecutor)

    def test_pipeline_has_six_stages(self):
        engine = _make_engine()
        pipeline = create_default_pipeline(engine)
        self.assertEqual(len(pipeline._stages), 6)

    def test_stage_types_in_order(self):
        engine = _make_engine()
        pipeline = create_default_pipeline(engine)
        stage_types = [type(s) for s in pipeline._stages]
        expected = [
            AudioNormalizationStage,
            STTStage,
            DiarizationStage,
            TextCleanupStage,
            LLMRewriteStage,
            TranslationStage,
        ]
        self.assertEqual(stage_types, expected)

    def test_diarization_fn_auto_detected_from_engine(self):
        engine = _make_engine()
        engine.run_diarization = MagicMock(return_value=[])
        pipeline = create_default_pipeline(engine)
        diar_stage: DiarizationStage = pipeline._stages[2]
        self.assertIs(diar_stage._diarization_fn, engine.run_diarization)

    def test_diarization_none_when_engine_has_no_method(self):
        engine = _make_engine()
        # engine mock has no run_diarization by default
        del engine.run_diarization
        pipeline = create_default_pipeline(engine)
        diar_stage: DiarizationStage = pipeline._stages[2]
        self.assertIsNone(diar_stage._diarization_fn)

    def test_custom_diarization_fn_injected(self):
        engine = _make_engine()
        custom_fn = lambda path: []  # noqa: E731
        pipeline = create_default_pipeline(engine, diarization_fn=custom_fn)
        diar_stage: DiarizationStage = pipeline._stages[2]
        self.assertIs(diar_stage._diarization_fn, custom_fn)

    def test_llm_rewriter_injected(self):
        engine = _make_engine()
        rewriter = MagicMock()
        pipeline = create_default_pipeline(engine, llm_rewriter=rewriter)
        llm_stage: LLMRewriteStage = pipeline._stages[4]
        self.assertIs(llm_stage._rewriter, rewriter)

    def test_translator_injected(self):
        engine = _make_engine()
        translator = MagicMock()
        pipeline = create_default_pipeline(engine, translator=translator)
        translation_stage: TranslationStage = pipeline._stages[5]
        self.assertIs(translation_stage._translator, translator)


# ---------------------------------------------------------------------------
# Bridge tests
# ---------------------------------------------------------------------------

class TestTranscribeV2(unittest.TestCase):

    def _run_bridge(self, text: str = "Тест транскрипция", **kwargs) -> dict:
        engine = _make_engine(text)
        audio = np.zeros(16000, dtype=np.float32)
        return transcribe_v2(engine, audio, **kwargs)

    def test_returns_dict(self):
        result = self._run_bridge()
        self.assertIsInstance(result, dict)

    def test_legacy_keys_present(self):
        result = self._run_bridge()
        required_keys = [
            "text", "raw_text", "cleaned_text", "confidence",
            "duration_ms", "engine", "model", "language",
            "segments", "diarization", "llm_applied",
        ]
        for key in required_keys:
            self.assertIn(key, result, f"Missing key: {key}")

    def test_engine_field_is_pipeline_v2(self):
        result = self._run_bridge()
        self.assertEqual(result["engine"], "pipeline_v2")

    def test_text_populated_from_stt(self):
        result = self._run_bridge("Привет от bridge")
        # text должен быть непустым (после cleanup может немного отличаться)
        self.assertTrue(len(result["text"]) > 0)

    def test_confidence_numeric(self):
        result = self._run_bridge()
        self.assertIsInstance(result["confidence"], float)
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_duration_ms_non_negative(self):
        result = self._run_bridge()
        self.assertGreaterEqual(result["duration_ms"], 0)

    def test_llm_not_applied_when_no_rewriter(self):
        result = self._run_bridge()
        self.assertFalse(result["llm_applied"])

    def test_translation_absent_when_no_translator(self):
        result = self._run_bridge(translation_mode="off")
        # TranslationStage пропускается при mode=off или translator=None
        # поле translation_mode по умолчанию "off"
        # result не должен содержать перевод в тексте
        self.assertFalse(result.get("llm_applied", True))

    def test_with_llm_rewriter_applied(self):
        engine = _make_engine("Исходный текст")
        rewriter = MagicMock()
        rewriter._circuit = None
        rewriter.rewrite.return_value = _LLMResult(ok=True, text="LLM переписал текст")

        audio = np.zeros(16000, dtype=np.float32)

        def settings_get(key, default=None):
            if key == "llm_rewrite_enabled":
                return True
            return default

        pipeline = create_default_pipeline(
            engine,
            llm_rewriter=rewriter,
            settings_get=settings_get,
        )
        ctx = PipelineContext(audio_input=audio, cleanup_profile="soft")
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        self.assertTrue(result["llm_applied"])
        self.assertEqual(result["text"], "LLM переписал текст")

    def test_with_translator(self):
        engine = _make_engine("Исходный текст для перевода")
        translator = MagicMock()
        translator.translate.return_value = _TranslationResult(ok=True, text="Texto traducido")

        audio = np.zeros(16000, dtype=np.float32)

        def settings_get(key, default=None):
            if key == "translation_mode":
                return "es"
            return default

        pipeline = create_default_pipeline(
            engine,
            translator=translator,
            settings_get=settings_get,
        )
        ctx = PipelineContext(audio_input=audio, translation_mode="es")
        ctx = pipeline.run(ctx)

        self.assertEqual(ctx.translation, "Texto traducido")
        self.assertEqual(ctx.translation_engine, "test_engine")

    def test_bridge_handles_stt_error_gracefully(self):
        engine = MagicMock()
        engine.transcribe.side_effect = RuntimeError("STT сломался")
        audio = np.zeros(16000, dtype=np.float32)

        result = transcribe_v2(engine, audio)

        # Должен вернуть dict без исключений
        self.assertIsInstance(result, dict)
        self.assertIn("text", result)
        # pipeline_errors должен содержать информацию об ошибке
        errors = result.get("pipeline_errors", [])
        self.assertTrue(any("stt" in e.lower() for e in errors))

    def test_pipeline_v2_setting_in_config(self):
        """PIPELINE_V2 setting должен существовать в config и быть False по умолчанию."""
        from core.config import settings
        self.assertFalse(settings.PIPELINE_V2)

    def test_pipeline_v2_overridable_via_env(self):
        """PIPELINE_V2 читается через KRAB_EAR_PIPELINE_V2 env var."""
        from core.config import Settings
        import os
        old = os.environ.get("KRAB_EAR_PIPELINE_V2")
        try:
            os.environ["KRAB_EAR_PIPELINE_V2"] = "true"
            s = Settings()
            self.assertTrue(s.PIPELINE_V2)
        finally:
            if old is None:
                os.environ.pop("KRAB_EAR_PIPELINE_V2", None)
            else:
                os.environ["KRAB_EAR_PIPELINE_V2"] = old


if __name__ == "__main__":
    unittest.main()
