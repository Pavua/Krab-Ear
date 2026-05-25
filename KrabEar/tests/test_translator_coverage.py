"""Coverage tests for KrabEar/backend/translator.py — offline-first translator.

All tests are pure unit tests: external transformer pipelines are mocked via
patching Translator._build_pipeline so no local models are loaded.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_builder(output: str):
    """Return a Translator._build_pipeline replacement that always returns `output`."""
    def builder(model_name: str, allow_network: bool):
        def pipeline(text: str):
            return [{"translation_text": output}]
        return pipeline
    return builder


def _passthrough_builder():
    """Pipeline that echoes the input prefixed with 'TR:'."""
    def builder(model_name: str, allow_network: bool):
        def pipeline(text: str):
            return [{"translation_text": f"TR:{text}"}]
        return pipeline
    return builder


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TranslatorCoverageTestCase(unittest.TestCase):
    """Comprehensive unit tests for Translator with mocked backends."""

    # ------------------------------------------------------------------
    # test_translate_ru_to_es
    # ------------------------------------------------------------------
    def test_translate_ru_to_es(self) -> None:
        """ru_to_es mode selects Helsinki-NLP/opus-mt-ru-es and returns ok status."""
        translator = Translator()
        used_models: list[str] = []
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            used_models.append(model_name)

            def pipeline(text: str):
                return [{"translation_text": "hola mundo"}]

            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            result = translator.translate(
                "привет мир", mode="ru_to_es", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "hola mundo")
        self.assertEqual(result.source_lang, "ru")
        self.assertEqual(result.target_lang, "es")
        self.assertTrue(any("ru-es" in m for m in used_models))

    # ------------------------------------------------------------------
    # test_translate_es_to_ru
    # ------------------------------------------------------------------
    def test_translate_es_to_ru(self) -> None:
        """es_to_ru mode selects Helsinki-NLP/opus-mt-es-ru and returns ok status."""
        translator = Translator()
        used_models: list[str] = []
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            used_models.append(model_name)

            def pipeline(text: str):
                return [{"translation_text": "привет мир"}]

            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            result = translator.translate(
                "hola mundo", mode="es_to_ru", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "привет мир")
        self.assertEqual(result.source_lang, "es")
        self.assertEqual(result.target_lang, "ru")
        self.assertTrue(any("es-ru" in m for m in used_models))

    # ------------------------------------------------------------------
    # test_translate_en_to_ru
    # ------------------------------------------------------------------
    def test_translate_en_to_ru(self) -> None:
        """en_to_ru mode selects Helsinki-NLP/opus-mt-en-ru."""
        translator = Translator()
        used_models: list[str] = []
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            used_models.append(model_name)

            def pipeline(text: str):
                return [{"translation_text": "привет"}]

            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            result = translator.translate(
                "hello world", mode="en_to_ru", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "en")
        self.assertEqual(result.target_lang, "ru")
        self.assertTrue(any("en-ru" in m for m in used_models))

    # ------------------------------------------------------------------
    # test_translate_auto_detects_lang
    # ------------------------------------------------------------------
    def test_translate_auto_detects_lang(self) -> None:
        """auto mode detects source language and routes to the correct pair."""
        translator = Translator()
        ru_models: list[str] = []
        es_models: list[str] = []
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipeline(text: str):
                if "ru-es" in model_name:
                    ru_models.append(model_name)
                elif "es-ru" in model_name:
                    es_models.append(model_name)
                return [{"translation_text": f"OUT:{text}"}]
            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            # Russian input → should route to ru_to_es
            result_ru = translator.translate(
                "это русский текст", mode="auto", network_mode="offline_default"
            )
            # Spanish input → should route to es_to_ru
            result_es = translator.translate(
                "hola amigo como estas", mode="auto", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result_ru.status, "ok")
        self.assertEqual(result_ru.source_lang, "ru")
        self.assertTrue(ru_models, "Expected ru-es model for Russian input")

        self.assertEqual(result_es.status, "ok")
        self.assertEqual(result_es.source_lang, "es")
        self.assertTrue(es_models, "Expected es-ru model for Spanish input")

    # ------------------------------------------------------------------
    # test_translate_bilingual_mode
    # ------------------------------------------------------------------
    def test_translate_bilingual_mode(self) -> None:
        """bilingual_ru_es produces 'RU: ...' and 'ES: ...' output lines."""
        translator = Translator()
        original = Translator._build_pipeline
        Translator._build_pipeline = staticmethod(_fake_builder("TRANSLATED_TEXT"))
        try:
            result = translator.translate(
                "привет мир", mode="bilingual_ru_es", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.mode, "bilingual_ru_es")
        self.assertIn("RU:", result.text)
        self.assertIn("ES:", result.text)
        self.assertIn("привет мир", result.text)
        self.assertIn("TRANSLATED_TEXT", result.text)

    # ------------------------------------------------------------------
    # test_translate_caches_result
    # ------------------------------------------------------------------
    def test_translate_caches_result(self) -> None:
        """Identical call twice: pipeline invoked once, both results are identical."""
        translator = Translator()
        call_count = {"n": 0}
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipeline(text: str):
                call_count["n"] += 1
                return [{"translation_text": f"ES:{text}"}]
            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            first = translator.translate(
                "кэш тест", mode="ru_to_es", network_mode="offline_default"
            )
            second = translator.translate(
                "кэш тест", mode="ru_to_es", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "ok")
        self.assertEqual(first.text, second.text)
        self.assertEqual(call_count["n"], 1, "Pipeline should be called once; second hit must come from cache")

    # ------------------------------------------------------------------
    # test_translate_cache_eviction_at_limit
    # ------------------------------------------------------------------
    def test_translate_cache_eviction_at_limit(self) -> None:
        """Cache evicts oldest entries when capacity is exceeded."""
        translator = Translator()
        translator._cache_capacity = 5  # tiny capacity for the test

        original = Translator._build_pipeline
        Translator._build_pipeline = staticmethod(_passthrough_builder())
        try:
            # Fill cache beyond capacity
            for i in range(7):
                translator.translate(
                    f"text{i}", mode="ru_to_es", network_mode="offline_default"
                )
        finally:
            Translator._build_pipeline = original

        self.assertLessEqual(
            len(translator._cache),
            translator._cache_capacity,
            "Cache size must not exceed _cache_capacity",
        )

    # ------------------------------------------------------------------
    # test_translate_off_mode_returns_original
    # ------------------------------------------------------------------
    def test_translate_off_mode_returns_original(self) -> None:
        """off mode returns empty text with status not_requested without calling pipeline."""
        translator = Translator()
        pipeline_called = {"called": False}
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipeline(text: str):
                pipeline_called["called"] = True
                return [{"translation_text": "should not appear"}]
            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            result = translator.translate(
                "любой текст", mode="off", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "not_requested")
        self.assertEqual(result.text, "")
        self.assertFalse(result.ok)
        self.assertFalse(pipeline_called["called"])

    # ------------------------------------------------------------------
    # test_translate_empty_input_returns_empty
    # ------------------------------------------------------------------
    def test_translate_empty_input_returns_empty(self) -> None:
        """Empty string and whitespace-only input return status empty_text without calling pipeline."""
        translator = Translator()
        original = Translator._build_pipeline

        pipeline_called = {"called": False}

        def builder(model_name: str, allow_network: bool):
            def pipeline(text: str):
                pipeline_called["called"] = True
                return [{"translation_text": "should not appear"}]
            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            for bad_input in ("", "   ", "\t\n", "\r"):
                with self.subTest(input=repr(bad_input)):
                    result = translator.translate(
                        bad_input, mode="ru_to_es", network_mode="offline_default"
                    )
                    self.assertEqual(result.status, "empty_text")
                    self.assertEqual(result.text, "")
        finally:
            Translator._build_pipeline = original

        self.assertFalse(pipeline_called["called"])

    # ------------------------------------------------------------------
    # test_translate_handles_translator_exception
    # ------------------------------------------------------------------
    def test_translate_handles_translator_exception(self) -> None:
        """Pipeline raising an exception produces status=translate_error, ok==False."""
        translator = Translator()
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipeline(text: str):
                raise RuntimeError("simulated pipeline crash")
            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            result = translator.translate(
                "тест ошибки", mode="ru_to_es", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "translate_error")
        self.assertEqual(result.text, "")
        self.assertFalse(result.ok)

    # ------------------------------------------------------------------
    # test_translate_glossary_overrides_term
    # ------------------------------------------------------------------
    def test_translate_glossary_overrides_term(self) -> None:
        """Glossary replaces the specified term in translation output."""
        translator = Translator()
        original = Translator._build_pipeline
        # Pipeline always returns a string containing the term "cliente"
        Translator._build_pipeline = staticmethod(_fake_builder("El cliente fue amable"))
        try:
            result = translator.translate(
                "клиент был вежлив",
                mode="ru_to_es",
                network_mode="offline_default",
                glossary={"cliente": "kliyent"},
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "ok")
        self.assertIn("kliyent", result.text)
        self.assertNotIn("cliente", result.text)

    # ------------------------------------------------------------------
    # test_translate_strips_whitespace
    # ------------------------------------------------------------------
    def test_translate_strips_whitespace(self) -> None:
        """Leading/trailing whitespace in input is stripped before translation."""
        translator = Translator()
        received: list[str] = []
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipeline(text: str):
                received.append(text)
                return [{"translation_text": "output"}]
            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            result = translator.translate(
                "  привет  ", mode="ru_to_es", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.status, "ok")
        # The pipeline should receive the stripped text
        self.assertTrue(received, "Pipeline was not called")
        self.assertEqual(received[0], "привет")

    # ------------------------------------------------------------------
    # test_translate_invalid_mode_falls_back
    # ------------------------------------------------------------------
    def test_translate_invalid_mode_falls_back(self) -> None:
        """Unknown/invalid mode is normalised to 'off' (status not_requested)."""
        translator = Translator()
        original = Translator._build_pipeline
        pipeline_called = {"called": False}

        def builder(model_name: str, allow_network: bool):
            def pipeline(text: str):
                pipeline_called["called"] = True
                return [{"translation_text": "should not appear"}]
            return pipeline

        Translator._build_pipeline = staticmethod(builder)
        try:
            result = translator.translate(
                "тест", mode="totally_invalid_mode_xyz", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original

        self.assertEqual(result.mode, "off")
        self.assertEqual(result.status, "not_requested")
        self.assertFalse(pipeline_called["called"])


if __name__ == "__main__":
    unittest.main()
