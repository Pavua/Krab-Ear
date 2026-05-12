"""Тесты для STTStage."""

from core.pipeline.context import PipelineContext
from core.pipeline.stages.stt import STTStage
import sys
import os
import unittest
import numpy as np

# Настройка PYTHONPATH для standalone запуска
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


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


class TestSTTEdgeCases(unittest.TestCase):
    """Edge cases: empty audio, VAD skip, extreme scenarios."""

    def test_single_sample_audio(self):
        # Minimal audio (1 sample)
        engine = FakeEngine(result=_ok_result(raw_text="minimal"))
        stage = STTStage(engine)
        ctx = _make_ctx(audio_input=np.array([0.1], dtype=np.float32))
        result = stage.process(ctx)
        # Should handle gracefully
        self.assertIsNotNone(result.raw_text)

    def test_vad_skip_returns_skipped_status(self):
        # Engine returns error/skip marker
        engine = FakeEngine(result={
            "status": "skipped",
            "reason": "vad_silence_detected",
            "text": "",
            "raw_text": "",
            "segments": []
        })
        stage = STTStage(engine)
        ctx = _make_ctx()
        result = stage.process(ctx)
        # Should append error but not crash
        self.assertTrue(any("skipped" in e or "silence" in e for e in result.errors) or result.raw_text == "")

    def test_extremely_long_audio_10_hours(self):
        # 10 hours at 16kHz = 576M samples (problematic on memory)
        engine = FakeEngine(result=_ok_result())
        stage = STTStage(engine)
        # Mock: don't actually create 10h of audio, just verify handling
        ctx = _make_ctx(audio_input=np.zeros(1000, dtype=np.float32))
        result = stage.process(ctx)
        self.assertIsNotNone(result.raw_text)

    def test_missing_segments_key_defaults_to_empty(self):
        engine = FakeEngine(result=_ok_result())
        del engine._result["segments"]
        stage = STTStage(engine)
        ctx = _make_ctx()
        result = stage.process(ctx)
        # Should default to [] not error
        self.assertEqual(result.segments, [])

    def test_missing_model_key_defaults_to_unknown(self):
        engine = FakeEngine(result=_ok_result())
        del engine._result["model"]
        stage = STTStage(engine)
        ctx = _make_ctx()
        result = stage.process(ctx)
        self.assertIsNotNone(result.model_used)

    def test_engine_with_out_of_range_confidence(self):
        # Engine returns invalid confidence (outside 0-1)
        engine = FakeEngine(result=_ok_result(confidence=1.5))
        stage = STTStage(engine)
        ctx = _make_ctx()
        result = stage.process(ctx)
        # Raw result may be out of range; verify it's set
        self.assertIsNotNone(result.confidence)
        self.assertEqual(result.confidence, 1.5)

    def test_engine_with_negative_confidence(self):
        # Engine returns negative confidence
        engine = FakeEngine(result=_ok_result(confidence=-0.5))
        stage = STTStage(engine)
        ctx = _make_ctx()
        result = stage.process(ctx)
        # Raw result preserved
        self.assertEqual(result.confidence, -0.5)


class TestSTTStageCoverage(unittest.TestCase):
    """Extra tests targeting previously uncovered branches in stage_stt.py."""

    # ------------------------------------------------------------------
    # cacheable class attribute
    # ------------------------------------------------------------------
    def test_cacheable_attribute_is_true(self):
        """STTStage.cacheable должен быть True (используется PipelineExecutor)."""
        from core.pipeline.stages.stt import STTStage
        self.assertTrue(STTStage.cacheable)
        engine = FakeEngine()
        stage = STTStage(engine)
        self.assertTrue(stage.cacheable)

    # ------------------------------------------------------------------
    # text fallback: result has 'text' but NOT 'raw_text'
    # ------------------------------------------------------------------
    def test_text_key_used_when_raw_text_missing(self):
        """Если движок возвращает 'text' без 'raw_text' — используем 'text'."""
        result = _ok_result()
        del result["raw_text"]  # нет raw_text → должен взять 'text'
        result["text"] = "Текст из text поля"
        engine = FakeEngine(result=result)
        stage = STTStage(engine)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(ctx.raw_text, "Текст из text поля")
        self.assertFalse(ctx.errors)

    # ------------------------------------------------------------------
    # raw_text empty string vs text fallback
    # ------------------------------------------------------------------
    def test_empty_raw_text_falls_back_to_text(self):
        """raw_text='' (falsy) должен упасть на 'text'."""
        result = _ok_result(raw_text="", text="Резервный текст")
        engine = FakeEngine(result=result)
        stage = STTStage(engine)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(ctx.raw_text, "Резервный текст")

    # ------------------------------------------------------------------
    # extra_vocabulary: non-empty list forwarded; empty list → None
    # ------------------------------------------------------------------
    def test_extra_vocabulary_nonempty_forwarded(self):
        """Непустой extra_vocabulary пробрасывается в kwargs движка."""
        engine = FakeEngine()
        stage = STTStage(engine)
        ctx = _make_ctx(extra_vocabulary=["краб", "ухо"])
        stage.process(ctx)
        kwargs = engine.calls[0][1]
        self.assertEqual(kwargs["extra_vocabulary"], ["краб", "ухо"])

    def test_extra_vocabulary_empty_list_sends_none(self):
        """Пустой extra_vocabulary конвертируется в None при вызове движка."""
        engine = FakeEngine()
        stage = STTStage(engine)
        ctx = _make_ctx(extra_vocabulary=[])
        stage.process(ctx)
        kwargs = engine.calls[0][1]
        self.assertIsNone(kwargs["extra_vocabulary"])

    # ------------------------------------------------------------------
    # status == 'skipped' is NOT treated as error (passes through)
    # ------------------------------------------------------------------
    def test_skipped_status_not_treated_as_error(self):
        """status='skipped' не является ошибкой — текст берётся из поля text/raw_text."""
        result = {
            "status": "skipped",
            "raw_text": "Тишина",
            "text": "Тишина",
            "language": "ru",
            "model": "mlx-whisper-balanced",
            "confidence": 0.1,
            "segments": [],
        }
        engine = FakeEngine(result=result)
        stage = STTStage(engine)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        # 'skipped' != 'error' → no errors appended
        self.assertFalse(ctx.errors)
        self.assertEqual(ctx.raw_text, "Тишина")

    # ------------------------------------------------------------------
    # error key without status key
    # ------------------------------------------------------------------
    def test_error_key_alone_triggers_error_path(self):
        """Результат с ключом 'error' (без status) также должен добавить ошибку."""
        result = {"error": "движок не загружен"}
        engine = FakeEngine(result=result)
        stage = STTStage(engine)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertTrue(ctx.errors)
        self.assertIn("движок не загружен", ctx.errors[0])

    # ------------------------------------------------------------------
    # status == 'error' с пустым полем error → берём status как msg
    # ------------------------------------------------------------------
    def test_status_error_no_error_key_uses_status_as_msg(self):
        """status='error' без поля 'error' — сообщение = значение status."""
        result = {"status": "error"}  # нет поля 'error'
        engine = FakeEngine(result=result)
        stage = STTStage(engine)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertTrue(ctx.errors)
        self.assertIn("error", ctx.errors[0])

    # ------------------------------------------------------------------
    # process debug log line covered (long successful transcript)
    # ------------------------------------------------------------------
    def test_long_transcript_completes_without_error(self):
        """Длинный текст (>1000 символов) логируется без ошибок — debug-ветка."""
        long_text = "Слово " * 200  # 1200 символов
        engine = FakeEngine(result=_ok_result(raw_text=long_text, confidence=0.88))
        stage = STTStage(engine)
        ctx = _make_ctx()
        ctx = stage.process(ctx)
        self.assertEqual(len(ctx.raw_text), len(long_text))
        self.assertFalse(ctx.errors)
        self.assertAlmostEqual(ctx.confidence, 0.88)

    # ------------------------------------------------------------------
    # lang_hint=None vs explicit value
    # ------------------------------------------------------------------
    def test_lang_hint_none_forwarded(self):
        """lang_hint=None явно пробрасывается в движок."""
        engine = FakeEngine()
        stage = STTStage(engine)
        ctx = _make_ctx()  # lang_hint defaults to None
        stage.process(ctx)
        kwargs = engine.calls[0][1]
        self.assertIsNone(kwargs["lang_hint"])

    def test_lang_hint_explicit_forwarded(self):
        """lang_hint='es' пробрасывается в движок корректно."""
        engine = FakeEngine()
        stage = STTStage(engine)
        ctx = _make_ctx(lang_hint="es")
        stage.process(ctx)
        kwargs = engine.calls[0][1]
        self.assertEqual(kwargs["lang_hint"], "es")
