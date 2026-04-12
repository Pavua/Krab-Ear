"""Тесты для STTStage."""

import sys
import os
import unittest
from unittest.mock import MagicMock
import numpy as np

# Настройка PYTHONPATH для standalone запуска
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.stages.stt import STTStage
from core.pipeline.context import PipelineContext


def _make_ctx(**kwargs) -> PipelineContext:
    audio = kwargs.pop("audio_input", np.zeros(16000, dtype=np.float32))
    return PipelineContext(audio_input=audio, **kwargs)


def _ok_result(**overrides) -> dict:
    base = {
        "text": "Привет мир",
        "raw_text": "Привет мир",
        "cleaned_text": "Привет мир",
        "language": "ru",
        "model": "mlx-whisper-balanced",
        "confidence": 0.95,
        "segments": [{"avg_logprob": -0.05}],
        "llm_applied": False,
        "llm_latency_ms": None,
        "llm_fallback_reason": None,
        "duration_ms": 200,
        "engine": "mlx-whisper",
    }
    base.update(overrides)
    return base


class FakeEngine:
    """Минимальный stub движка STT для тестов."""
    def __init__(self, result=None, side_effect=None):
        self._result = result or _ok_result()
        self._side_effect = side_effect
        self.calls = []

    def transcribe(self, audio_data, **kwargs):
        self.calls.append((audio_data, kwargs))
        if self._side_effect:
            raise self._side_effect
        return self._result


class TestSTTStageBasic(unittest.TestCase):

    def _stage(self, result=None, side_effect=None):
        engine = FakeEngine(result=result, side_effect=side_effect)
        return STTStage(engine), engine

    def test_basic_transcription_sets_raw_text(self):
        stage, fn = self._stage()
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(ctx.raw_text, "Привет мир")
        self.assertFalse(ctx.errors)

    def test_basic_transcription_sets_language(self):
        stage, _ = self._stage()
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(ctx.language_detected, "ru")

    def test_basic_transcription_sets_confidence(self):
        stage, _ = self._stage()
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertAlmostEqual(ctx.confidence, 0.95)

    def test_basic_transcription_sets_model_used(self):
        stage, _ = self._stage()
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(ctx.model_used, "mlx-whisper-balanced")

    def test_basic_transcription_sets_segments(self):
        stage, _ = self._stage()
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(len(ctx.segments), 1)

    def test_should_run_true_with_audio_input(self):
        stage, _ = self._stage()
        ctx = _make_ctx()
        self.assertTrue(stage.should_run(ctx))

    def test_should_run_true_with_normalized_audio(self):
        stage, _ = self._stage()
        ctx = _make_ctx(audio_input=None)
        ctx.normalized_audio = np.zeros(8000, dtype=np.float32)
        self.assertTrue(stage.should_run(ctx))

    def test_should_run_false_without_audio(self):
        stage, _ = self._stage()
        ctx = _make_ctx(audio_input=None)
        ctx.normalized_audio = None
        self.assertFalse(stage.should_run(ctx))

    def test_prefers_normalized_audio_over_audio_input(self):
        stage, engine = self._stage()
        ctx = _make_ctx()
        norm = np.ones(16000, dtype=np.float32)
        ctx.normalized_audio = norm
        stage.process(ctx)
        self.assertEqual(len(engine.calls), 1)
        call_arg = engine.calls[0][0]
        np.testing.assert_array_equal(call_arg, norm)

    def test_engine_exception_appends_to_errors_no_raise(self):
        stage, _ = self._stage(side_effect=RuntimeError("VRAM exhausted"))  # type: ignore
        ctx = _make_ctx()
        result = stage.process(ctx)
        self.assertIn("stt:", result.errors[0])
        self.assertIn("VRAM exhausted", result.errors[0])
        self.assertEqual(result.raw_text, "")

    def test_engine_returns_error_dict_appends_to_errors(self):
        stage, _ = self._stage(result={"status": "error", "error": "all engines down"})
        ctx = _make_ctx()
        result = stage.process(ctx)
        self.assertTrue(result.errors)
        self.assertIn("all engines down", result.errors[0])

    def test_stage_name_is_stt(self):
        stage, _ = self._stage()
        self.assertEqual(stage.name, "stt")

    def test_transcribe_fn_called_with_ctx_params(self):
        stage, engine = self._stage()
        ctx = _make_ctx(cleanup_profile="strict", is_preview=True, domain="meeting", lang_hint="ru")
        stage.process(ctx)
        self.assertEqual(len(engine.calls), 1)
        kwargs = engine.calls[0][1]
        self.assertEqual(kwargs["cleanup_profile"], "strict")
        self.assertTrue(kwargs["is_preview"])
        self.assertEqual(kwargs["domain"], "meeting")
        self.assertEqual(kwargs["lang_hint"], "ru")

    def test_engine_object_uses_transcribe_method(self):
        engine = FakeEngine(result=_ok_result(raw_text="Hello"))
        stage = STTStage(engine)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(ctx.raw_text, "Hello")

    def test_callable_engine_used_directly(self):
        calls = []

        def fake_transcribe(audio_data, **kwargs):
            calls.append((audio_data, kwargs))
            return _ok_result(raw_text="Direct")

        stage = STTStage(fake_transcribe)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.raw_text, "Direct")

    def test_zero_confidence_when_missing(self):
        result = _ok_result()
        del result["confidence"]
        stage, _ = self._stage(result=result)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(ctx.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
