"""Tests for TranslationStage."""

from core.pipeline.stages.translation import TranslationStage
from core.pipeline.context import PipelineContext
import sys
import os
import unittest
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@dataclass
class FakeTranslationResult:
    text: str
    status: str
    source_lang: str = "ru"
    target_lang: str = "es"
    mode: str = "ru_to_es"
    engine: str = "opus-mt"

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.text.strip())


class FakeTranslator:
    def __init__(self, result: FakeTranslationResult):
        self._result = result
        self.calls = []

    def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
        self.calls.append({"text": text, "mode": mode, "network_mode": network_mode})
        return self._result


class ErrorTranslator:
    def translate(self, **kwargs):
        raise RuntimeError("translation model unavailable")


def make_ctx(**kwargs) -> PipelineContext:
    defaults = dict(audio_input=None)
    defaults.update(kwargs)
    return PipelineContext(**defaults)


class TestTranslationStageInit(unittest.TestCase):
    def test_name(self):
        stage = TranslationStage(translator=None)
        self.assertEqual(stage.name, "translation")

    def test_none_translator_should_not_run(self):
        stage = TranslationStage(translator=None)
        ctx = make_ctx(translation_mode="ru_to_es")
        self.assertFalse(stage.should_run(ctx))


class TestTranslationStageShouldRun(unittest.TestCase):
    def _make_stage(self, settings_get=None):
        translator = FakeTranslator(FakeTranslationResult("hola", "ok"))
        return TranslationStage(translator=translator, settings_get=settings_get)

    def test_off_mode_in_ctx(self):
        stage = self._make_stage()
        ctx = make_ctx(translation_mode="off")
        self.assertFalse(stage.should_run(ctx))

    def test_active_mode_in_ctx(self):
        stage = self._make_stage()
        ctx = make_ctx(translation_mode="ru_to_es")
        self.assertTrue(stage.should_run(ctx))

    def test_off_mode_from_settings(self):
        stage = self._make_stage(settings_get=lambda k, d=None: "off" if k == "translation_mode" else d)
        ctx = make_ctx(translation_mode="")
        self.assertFalse(stage.should_run(ctx))

    def test_active_mode_from_settings(self):
        stage = self._make_stage(settings_get=lambda k, d=None: "es_to_ru" if k == "translation_mode" else d)
        ctx = make_ctx(translation_mode="")
        self.assertTrue(stage.should_run(ctx))


class TestTranslationStageProcess(unittest.TestCase):
    def _run(self, result, ctx):
        translator = FakeTranslator(result)
        stage = TranslationStage(translator=translator)
        return stage.process(ctx), translator

    def test_successful_translation_sets_fields(self):
        ctx = make_ctx(translation_mode="ru_to_es", final_text="Привет мир")
        res = FakeTranslationResult("Hola mundo", "ok")
        ctx, _ = self._run(res, ctx)
        self.assertEqual(ctx.translation, "Hola mundo")
        self.assertEqual(ctx.translation_engine, "opus-mt")
        self.assertEqual(len(ctx.errors), 0)

    def test_uses_final_text_first(self):
        ctx = make_ctx(translation_mode="ru_to_es", final_text="final", cleaned_text="clean", raw_text="raw")
        translator = FakeTranslator(FakeTranslationResult("translated", "ok"))
        TranslationStage(translator=translator).process(ctx)
        self.assertEqual(translator.calls[0]["text"], "final")

    def test_falls_back_to_cleaned_text(self):
        ctx = make_ctx(translation_mode="ru_to_es", final_text="", cleaned_text="clean", raw_text="raw")
        translator = FakeTranslator(FakeTranslationResult("translated", "ok"))
        TranslationStage(translator=translator).process(ctx)
        self.assertEqual(translator.calls[0]["text"], "clean")

    def test_falls_back_to_raw_text(self):
        ctx = make_ctx(translation_mode="ru_to_es", final_text="", cleaned_text="", raw_text="raw")
        translator = FakeTranslator(FakeTranslationResult("translated", "ok"))
        TranslationStage(translator=translator).process(ctx)
        self.assertEqual(translator.calls[0]["text"], "raw")

    def test_failed_result_adds_to_errors(self):
        ctx = make_ctx(translation_mode="ru_to_es", final_text="hello")
        res = FakeTranslationResult("", "model_unavailable")
        ctx, _ = self._run(res, ctx)
        self.assertIsNone(ctx.translation)
        self.assertTrue(any("translation_failed" in e for e in ctx.errors))

    def test_exception_adds_to_errors_no_raise(self):
        ctx = make_ctx(translation_mode="ru_to_es", final_text="hello")
        stage = TranslationStage(translator=ErrorTranslator())
        result_ctx = stage.process(ctx)
        self.assertTrue(any("translation_unexpected" in e for e in result_ctx.errors))
        self.assertIsNone(result_ctx.translation)

    def test_settings_get_called_for_network_mode(self):
        called = {}

        def settings_get(k, d=None):
            called[k] = True
            return d
        ctx = make_ctx(translation_mode="ru_to_es", final_text="text")
        translator = FakeTranslator(FakeTranslationResult("ok_text", "ok"))
        stage = TranslationStage(translator=translator, settings_get=settings_get)
        stage.process(ctx)
        self.assertIn("network_mode", called)

    def test_returns_ctx(self):
        ctx = make_ctx(translation_mode="ru_to_es", final_text="hello")
        translator = FakeTranslator(FakeTranslationResult("hola", "ok"))
        stage = TranslationStage(translator=translator)
        result = stage.process(ctx)
        self.assertIs(result, ctx)


if __name__ == "__main__":
    unittest.main()
