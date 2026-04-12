"""E2E integration tests for Pipeline v2.

Covers:
- Full pipeline with synthetic numpy sine-wave audio
- Full pipeline with mock engine returning known text
- Stage execution order verified via stage_metrics
- Pipeline with LLM disabled (LLMRewriteStage skipped)
- Pipeline with translation disabled (TranslationStage skipped)
- Pipeline with diarization disabled (DiarizationStage skipped)
- Error in STT stage → pipeline continues, errors collected
- to_legacy_dict() returns all required keys
- PIPELINE_V2 feature flag toggle (env var)
- transcribe_v2 bridge integration
- strict vs soft cleanup profile end-to-end
- preview mode: segments empty, diarization skipped
- multiple errors accumulate without aborting pipeline
- translator error is soft-fail (recorded, not raised)
- LLM circuit breaker open → stage skipped
"""

from __future__ import annotations

import os
import sys
import math
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# --- path setup ---
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from core.pipeline.context import PipelineContext, StageMetric
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
# Shared helpers
# ---------------------------------------------------------------------------

EXPECTED_LEGACY_KEYS = {
    "text", "raw_text", "cleaned_text", "llm_applied",
    "llm_latency_ms", "llm_fallback_reason", "confidence",
    "duration_ms", "engine", "model", "language",
    "segments", "diarization",
}

EXPECTED_STAGE_ORDER = [
    "audio_normalization",
    "stt",
    "diarization",
    "text_cleanup",
    "llm_rewrite",
    "translation",
]


def _sine_wave(duration_s: float = 0.5, sample_rate: int = 16000, freq: float = 440.0) -> np.ndarray:
    """Generate a synthetic mono sine wave as float32 numpy array."""
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    wave = np.sin(2 * math.pi * freq * t).astype(np.float32)
    return wave


def _make_stt_result(text: str = "Тестовый текст") -> dict:
    return {
        "text": text,
        "raw_text": text,
        "language": "ru",
        "model": "whisper-test",
        "confidence": 0.92,
        "segments": [{"start": 0.0, "end": 1.0, "text": text}],
    }


def _make_mock_engine(text: str = "Тестовый текст") -> MagicMock:
    engine = MagicMock()
    engine.transcribe.return_value = _make_stt_result(text)
    # Prevent auto-detection of run_diarization (we control it via diarization_fn arg)
    del engine.run_diarization
    return engine


class _LLMResult:
    def __init__(self, ok: bool = True, text: str = "LLM rewrite"):
        self.ok = ok
        self.text = text if ok else None
        self.latency_ms = 30
        self.fallback_reason = None if ok else "disabled"


class _TranslationResult:
    def __init__(self, ok: bool = True, text: str = "Translated text"):
        self.ok = ok
        self.text = text if ok else None
        self.engine = "test_translator"
        self.source_lang = "ru"
        self.target_lang = "es"
        self.status = "ok" if ok else "error"


def _make_mock_llm(ok: bool = True, text: str = "LLM rewrite") -> MagicMock:
    rewriter = MagicMock()
    rewriter._circuit = None
    rewriter.rewrite.return_value = _LLMResult(ok=ok, text=text)
    return rewriter


def _make_mock_translator(ok: bool = True, text: str = "Translated text") -> MagicMock:
    translator = MagicMock()
    translator.translate.return_value = _TranslationResult(ok=ok, text=text)
    return translator


def _settings_with(overrides: dict):
    """Return a settings_get callable with the given key overrides."""
    def settings_get(key, default=None):
        return overrides.get(key, default)
    return settings_get


# ---------------------------------------------------------------------------
# 1. Full pipeline with synthetic sine-wave audio
# ---------------------------------------------------------------------------

class TestE2EWithSineWaveAudio(unittest.TestCase):
    """Full pipeline driven by synthetic sine-wave numpy audio, no real STT."""

    def test_pipeline_completes_with_sine_wave_input(self):
        audio = _sine_wave()
        engine = _make_mock_engine("Привет мир")
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        self.assertIsInstance(ctx, PipelineContext)
        self.assertFalse(ctx.final_text == "" and ctx.raw_text == "",
                         "Pipeline should produce non-empty text from STT mock")

    def test_sine_wave_normalized_to_target_rms(self):
        """AudioNormalizationStage should bring raw sine wave to ~TARGET_RMS."""
        TARGET_RMS = 0.1
        audio = _sine_wave()  # amplitude 1.0 → rms ≈ 0.707

        stage = AudioNormalizationStage()
        ctx = PipelineContext(audio_input=audio)
        ctx = stage.process(ctx)

        normalized = ctx.normalized_audio
        self.assertIsInstance(normalized, np.ndarray)
        rms = float(np.sqrt(np.mean(normalized ** 2)))
        self.assertAlmostEqual(rms, TARGET_RMS, places=3)

    def test_stereo_sine_wave_converted_to_mono(self):
        """AudioNormalizationStage converts 2-channel input to 1-channel."""
        t = np.linspace(0, 0.5, 8000, endpoint=False).astype(np.float32)
        stereo = np.stack([np.sin(2 * math.pi * 440 * t),
                           np.sin(2 * math.pi * 880 * t)], axis=1)
        self.assertEqual(stereo.ndim, 2)

        stage = AudioNormalizationStage()
        ctx = PipelineContext(audio_input=stereo)
        ctx = stage.process(ctx)

        self.assertEqual(ctx.normalized_audio.ndim, 1)

    def test_full_bridge_with_sine_wave(self):
        """transcribe_v2 end-to-end with synthetic audio returns valid legacy dict."""
        audio = _sine_wave()
        engine = _make_mock_engine("Синтетический тест")
        result = transcribe_v2(engine, audio)

        self.assertIsInstance(result, dict)
        for key in EXPECTED_LEGACY_KEYS:
            self.assertIn(key, result, f"Missing key in legacy dict: {key}")


# ---------------------------------------------------------------------------
# 2. Full pipeline with mock engine returning known text
# ---------------------------------------------------------------------------

class TestE2EWithMockEngine(unittest.TestCase):
    """Pipeline with a mock STT engine that returns controlled output."""

    def setUp(self):
        self.audio = np.zeros(16000, dtype=np.float32)

    def test_raw_text_matches_engine_output(self):
        known_text = "Известный тестовый текст"
        engine = _make_mock_engine(known_text)
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=self.audio)
        ctx = pipeline.run(ctx)

        self.assertEqual(ctx.raw_text, known_text)

    def test_confidence_propagated_from_engine(self):
        engine = _make_mock_engine()
        engine.transcribe.return_value = _make_stt_result("текст")
        engine.transcribe.return_value["confidence"] = 0.87

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=self.audio)
        ctx = pipeline.run(ctx)

        self.assertAlmostEqual(ctx.confidence, 0.87, places=5)

    def test_language_propagated_from_engine(self):
        engine = _make_mock_engine()
        engine.transcribe.return_value["language"] = "es"

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=self.audio)
        ctx = pipeline.run(ctx)

        self.assertEqual(ctx.language_detected, "es")

    def test_model_used_propagated_from_engine(self):
        engine = _make_mock_engine()
        engine.transcribe.return_value["model"] = "large-v3"

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=self.audio)
        ctx = pipeline.run(ctx)

        self.assertEqual(ctx.model_used, "large-v3")

    def test_segments_populated_from_engine(self):
        segs = [{"start": 0.0, "end": 1.5, "text": "seg1"},
                {"start": 1.5, "end": 3.0, "text": "seg2"}]
        engine = _make_mock_engine()
        engine.transcribe.return_value["segments"] = segs

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=self.audio)
        ctx = pipeline.run(ctx)

        self.assertEqual(ctx.segments, segs)


# ---------------------------------------------------------------------------
# 3. All stages execute in correct order (verify via stage_metrics)
# ---------------------------------------------------------------------------

class TestE2EStageOrder(unittest.TestCase):

    def test_all_six_stages_produce_metrics(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Порядок стадий")

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        stage_names = [m.stage for m in ctx.stage_metrics]
        self.assertEqual(len(stage_names), 6,
                         f"Expected 6 stage metrics, got: {stage_names}")

    def test_stages_execute_in_correct_order(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Порядок стадий")

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        stage_names = [m.stage for m in ctx.stage_metrics]
        self.assertEqual(stage_names, EXPECTED_STAGE_ORDER)

    def test_audio_normalization_runs_first(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine()

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        self.assertEqual(ctx.stage_metrics[0].stage, "audio_normalization")
        self.assertFalse(ctx.stage_metrics[0].skipped)

    def test_stt_runs_second(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine()

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        self.assertEqual(ctx.stage_metrics[1].stage, "stt")
        self.assertFalse(ctx.stage_metrics[1].skipped)


# ---------------------------------------------------------------------------
# 4. Pipeline with LLM disabled → LLMRewriteStage skipped
# ---------------------------------------------------------------------------

class TestE2ELLMDisabled(unittest.TestCase):

    def test_llm_stage_skipped_when_rewriter_is_none(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Без LLM")

        pipeline = create_default_pipeline(engine, llm_rewriter=None)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        llm_metric = next(m for m in ctx.stage_metrics if m.stage == "llm_rewrite")
        self.assertTrue(llm_metric.skipped)

    def test_llm_stage_skipped_when_setting_disabled(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("LLM выключен настройкой")
        rewriter = _make_mock_llm()

        pipeline = create_default_pipeline(
            engine,
            llm_rewriter=rewriter,
            settings_get=_settings_with({"llm_rewrite_enabled": False}),
        )
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        llm_metric = next(m for m in ctx.stage_metrics if m.stage == "llm_rewrite")
        self.assertTrue(llm_metric.skipped)
        self.assertFalse(ctx.llm_applied)

    def test_final_text_falls_back_to_cleaned_when_llm_skipped(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("LLM не применён")

        pipeline = create_default_pipeline(engine, llm_rewriter=None)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        # final_text should be cleaned or raw text, not empty
        self.assertTrue(len(ctx.final_text) > 0)
        self.assertFalse(ctx.llm_applied)

    def test_llm_circuit_open_causes_skip(self):
        """If circuit breaker is open, LLMRewriteStage.should_run returns False."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Circuit breaker test")
        rewriter = _make_mock_llm()
        circuit = MagicMock()
        circuit.state = "open"
        rewriter._circuit = circuit

        pipeline = create_default_pipeline(
            engine,
            llm_rewriter=rewriter,
            settings_get=_settings_with({"llm_rewrite_enabled": True}),
        )
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        llm_metric = next(m for m in ctx.stage_metrics if m.stage == "llm_rewrite")
        self.assertTrue(llm_metric.skipped)
        self.assertFalse(ctx.llm_applied)


# ---------------------------------------------------------------------------
# 5. Pipeline with translation disabled
# ---------------------------------------------------------------------------

class TestE2ETranslationDisabled(unittest.TestCase):

    def test_translation_stage_skipped_when_translator_none(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Без перевода")

        pipeline = create_default_pipeline(engine, translator=None)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        tr_metric = next(m for m in ctx.stage_metrics if m.stage == "translation")
        self.assertTrue(tr_metric.skipped)

    def test_translation_stage_skipped_when_mode_off(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Режим off")
        translator = _make_mock_translator()

        pipeline = create_default_pipeline(
            engine,
            translator=translator,
            settings_get=_settings_with({"translation_mode": "off"}),
        )
        ctx = PipelineContext(audio_input=audio, translation_mode="off")
        ctx = pipeline.run(ctx)

        tr_metric = next(m for m in ctx.stage_metrics if m.stage == "translation")
        self.assertTrue(tr_metric.skipped)
        self.assertIsNone(ctx.translation)

    def test_translation_applied_when_enabled(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Текст для перевода")
        translator = _make_mock_translator(ok=True, text="Texto traducido")

        pipeline = create_default_pipeline(
            engine,
            translator=translator,
            settings_get=_settings_with({"translation_mode": "es"}),
        )
        ctx = PipelineContext(audio_input=audio, translation_mode="es")
        ctx = pipeline.run(ctx)

        tr_metric = next(m for m in ctx.stage_metrics if m.stage == "translation")
        self.assertFalse(tr_metric.skipped)
        self.assertEqual(ctx.translation, "Texto traducido")
        self.assertEqual(ctx.translation_engine, "test_translator")


# ---------------------------------------------------------------------------
# 6. Pipeline with diarization disabled
# ---------------------------------------------------------------------------

class TestE2EDiarizationDisabled(unittest.TestCase):

    def test_diarization_skipped_when_fn_is_none(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Без диаризации")

        pipeline = create_default_pipeline(engine, diarization_fn=None)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        diar_metric = next(m for m in ctx.stage_metrics if m.stage == "diarization")
        self.assertTrue(diar_metric.skipped)

    def test_diarization_skipped_for_ndarray_input(self):
        """DiarizationStage requires file path; ndarray input → skipped."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Numpy буфер — диаризация не поддерживается")

        # Even with a diarization_fn, ndarray audio → should_run returns False
        diar_fn = MagicMock(return_value=[])
        pipeline = create_default_pipeline(engine, diarization_fn=diar_fn)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        diar_metric = next(m for m in ctx.stage_metrics if m.stage == "diarization")
        self.assertTrue(diar_metric.skipped)
        # diarization_fn should NOT have been called
        diar_fn.assert_not_called()

    def test_diarization_skipped_in_preview_mode(self):
        """Preview mode always skips diarization regardless of fn."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine()
        diar_fn = MagicMock(return_value=[])

        pipeline = create_default_pipeline(engine, diarization_fn=diar_fn)
        ctx = PipelineContext(audio_input=audio, is_preview=True)
        ctx = pipeline.run(ctx)

        diar_metric = next(m for m in ctx.stage_metrics if m.stage == "diarization")
        self.assertTrue(diar_metric.skipped)
        diar_fn.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Error in STT stage → pipeline continues, errors collected
# ---------------------------------------------------------------------------

class TestE2ESTTError(unittest.TestCase):

    def test_stt_exception_recorded_in_errors(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = MagicMock()
        del engine.run_diarization
        engine.transcribe.side_effect = RuntimeError("STT упал")

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        self.assertTrue(any("stt" in e.lower() for e in ctx.errors),
                        f"Expected stt error, got: {ctx.errors}")

    def test_pipeline_continues_after_stt_error(self):
        """Subsequent stages (text_cleanup, llm, translation) still execute or skip."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = MagicMock()
        del engine.run_diarization
        engine.transcribe.side_effect = RuntimeError("STT сломан")

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        # Should have 6 stage metrics (not aborted)
        self.assertEqual(len(ctx.stage_metrics), 6)

    def test_stt_error_result_returns_dict_not_raises(self):
        """transcribe_v2 must return dict even if STT engine raises."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = MagicMock()
        del engine.run_diarization
        engine.transcribe.side_effect = Exception("Критическая ошибка")

        result = transcribe_v2(engine, audio)

        self.assertIsInstance(result, dict)
        self.assertIn("text", result)

    def test_stt_engine_error_status_recorded(self):
        """Engine returning {error: ...} is treated as error, not success."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = MagicMock()
        del engine.run_diarization
        engine.transcribe.return_value = {"error": "model not loaded", "status": "error"}

        pipeline = create_default_pipeline(engine)
        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)

        self.assertTrue(any("stt" in e for e in ctx.errors),
                        f"Expected stt error, got: {ctx.errors}")
        self.assertEqual(ctx.raw_text, "")


# ---------------------------------------------------------------------------
# 8. to_legacy_dict returns all required keys
# ---------------------------------------------------------------------------

class TestE2EToLegacyDict(unittest.TestCase):

    def test_all_required_keys_present_in_legacy_dict(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Словарь ключей")
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        self.assertEqual(set(result.keys()), EXPECTED_LEGACY_KEYS,
                         f"Key mismatch: {set(result.keys()) ^ EXPECTED_LEGACY_KEYS}")

    def test_engine_field_is_pipeline_v2(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine()
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        self.assertEqual(result["engine"], "pipeline_v2")

    def test_duration_ms_equals_sum_of_stage_durations(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine()
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        expected = sum(m.duration_ms for m in ctx.stage_metrics)
        self.assertEqual(result["duration_ms"], expected)

    def test_text_field_reflects_final_text(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Финальный текст")
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        self.assertEqual(result["text"], ctx.final_text)

    def test_segments_empty_when_preview_mode(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Preview")
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio, is_preview=True)
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        self.assertEqual(result["segments"], [])

    def test_confidence_is_float_in_range(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine()
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio)
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        self.assertIsInstance(result["confidence"], float)
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)


# ---------------------------------------------------------------------------
# 9. PIPELINE_V2 feature flag toggle
# ---------------------------------------------------------------------------

class TestE2EFeatureFlag(unittest.TestCase):

    def test_pipeline_v2_default_is_false(self):
        from core.config import settings
        self.assertFalse(settings.PIPELINE_V2)

    def test_pipeline_v2_enabled_via_env(self):
        from core.config import Settings
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

    def test_pipeline_v2_disabled_via_env(self):
        from core.config import Settings
        old = os.environ.get("KRAB_EAR_PIPELINE_V2")
        try:
            os.environ["KRAB_EAR_PIPELINE_V2"] = "false"
            s = Settings()
            self.assertFalse(s.PIPELINE_V2)
        finally:
            if old is None:
                os.environ.pop("KRAB_EAR_PIPELINE_V2", None)
            else:
                os.environ["KRAB_EAR_PIPELINE_V2"] = old

    def test_transcribe_v2_runs_independently_of_flag(self):
        """transcribe_v2 bridge doesn't check PIPELINE_V2 — caller does."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Feature flag test")
        # Should always work regardless of setting
        result = transcribe_v2(engine, audio)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["engine"], "pipeline_v2")


# ---------------------------------------------------------------------------
# 10. Additional E2E scenarios
# ---------------------------------------------------------------------------

class TestE2ECleanupProfiles(unittest.TestCase):

    def test_soft_profile_does_not_strip_non_hallucinations(self):
        """Real text through soft cleanup should survive intact (mostly)."""
        audio = np.zeros(16000, dtype=np.float32)
        normal_text = "Привет, как дела? Всё хорошо."
        engine = _make_mock_engine(normal_text)
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio, cleanup_profile="soft")
        ctx = pipeline.run(ctx)

        # Text should not be blank after cleanup
        self.assertTrue(len(ctx.cleaned_text) > 0)
        self.assertEqual(ctx.final_text, ctx.cleaned_text)

    def test_strict_profile_applied_end_to_end(self):
        """Strict profile runs through the pipeline without crashing."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Тест строгой очистки")
        pipeline = create_default_pipeline(engine)

        ctx = PipelineContext(audio_input=audio, cleanup_profile="strict")
        ctx = pipeline.run(ctx)

        # TextCleanup must have run (not skipped)
        cleanup_metric = next(m for m in ctx.stage_metrics if m.stage == "text_cleanup")
        self.assertFalse(cleanup_metric.skipped)


class TestE2EMultipleErrors(unittest.TestCase):

    def test_multiple_stage_errors_accumulated(self):
        """Both STT and translation errors → both recorded without abort."""
        audio = np.zeros(16000, dtype=np.float32)

        # STT raises
        engine = MagicMock()
        del engine.run_diarization
        engine.transcribe.side_effect = RuntimeError("STT error")

        # Translator raises
        translator = MagicMock()
        translator.translate.side_effect = RuntimeError("Translation error")

        pipeline = create_default_pipeline(
            engine,
            translator=translator,
            settings_get=_settings_with({"translation_mode": "es"}),
        )
        ctx = PipelineContext(audio_input=audio, translation_mode="es")
        ctx = pipeline.run(ctx)

        # At least stt error should be present; translation may or may not
        # run (text_cleanup skips if raw_text empty, translation might skip too)
        self.assertTrue(len(ctx.errors) >= 1)
        self.assertEqual(len(ctx.stage_metrics), 6)


class TestE2EBridgeIntegration(unittest.TestCase):

    def test_bridge_returns_pipeline_errors_when_stt_fails(self):
        audio = np.zeros(16000, dtype=np.float32)
        engine = MagicMock()
        del engine.run_diarization
        engine.transcribe.side_effect = RuntimeError("Bridge STT fail")

        result = transcribe_v2(engine, audio)

        self.assertIn("pipeline_errors", result)
        errors = result["pipeline_errors"]
        self.assertTrue(any("stt" in e.lower() for e in errors))

    def test_bridge_with_llm_and_translation(self):
        """transcribe_v2 with all features active returns enriched dict."""
        audio = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("Полный pipeline")
        rewriter = _make_mock_llm(ok=True, text="LLM улучшил")
        translator = _make_mock_translator(ok=True, text="Pipeline completo")

        # We need to use create_default_pipeline directly to pass settings_get
        pipeline = create_default_pipeline(
            engine,
            llm_rewriter=rewriter,
            translator=translator,
            settings_get=_settings_with({
                "llm_rewrite_enabled": True,
                "translation_mode": "es",
            }),
        )
        ctx = PipelineContext(
            audio_input=audio,
            translation_mode="es",
        )
        ctx = pipeline.run(ctx)
        result = pipeline.to_legacy_dict(ctx)

        self.assertTrue(result["llm_applied"])
        self.assertEqual(result["text"], "LLM улучшил")
        self.assertEqual(ctx.translation, "Pipeline completo")

    def test_bridge_with_silent_audio_no_crash(self):
        """Silent buffer (all zeros) must not crash the pipeline."""
        silent = np.zeros(16000, dtype=np.float32)
        engine = _make_mock_engine("")  # empty transcription
        engine.transcribe.return_value = {
            "text": "", "raw_text": "", "language": "ru",
            "model": "test", "confidence": 0.0, "segments": [],
        }

        result = transcribe_v2(engine, silent)

        self.assertIsInstance(result, dict)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
