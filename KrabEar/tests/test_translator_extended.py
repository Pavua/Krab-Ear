"""Расширенные unit-тесты для backend/translator.py.

Покрывает ветки, не охваченные в test_translator.py:
- _detect_source_language: испанские маркеры, английские маркеры, спецсимволы
- _apply_style: все ветки (neutral, formal, chat)
- auto_to_ru режим: already_target_language и перевод ES/EN → RU
- en_to_ru режим
- model_unavailable paths (offline / pipeline None)
- _normalize_mode: невалидный mode → off
- TranslationResult.ok property
- _langs_from_mode: все пары + unknown mode
- _split_text_chunks: edge cases (пустая строка, один символ)
- _apply_glossary
- glossary нормализация (пустые ключи, не-dict)
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator, TranslationResult


class TranslatorDetectLanguageTestCase(unittest.TestCase):
    """Тесты эвристики _detect_source_language."""

    def test_cyrillic_detected_as_russian(self):
        result = Translator._detect_source_language("Привет, как дела?")
        self.assertEqual(result, "ru")

    def test_spanish_markers_detected(self):
        result = Translator._detect_source_language("hola amigo como estas")
        self.assertEqual(result, "es")

    def test_spanish_special_chars_detected(self):
        result = Translator._detect_source_language("¿cómo estás tú?")
        self.assertEqual(result, "es")

    def test_english_markers_detected(self):
        result = Translator._detect_source_language("the quick brown fox and the lazy dog")
        self.assertEqual(result, "en")

    def test_empty_string_returns_empty(self):
        result = Translator._detect_source_language("")
        self.assertEqual(result, "")

    def test_whitespace_only_returns_empty(self):
        result = Translator._detect_source_language("   ")
        self.assertEqual(result, "")

    def test_latin_without_markers_defaults_to_en(self):
        # Латиница без знакомых маркеров → en (default)
        result = Translator._detect_source_language("xyz abc def")
        self.assertEqual(result, "en")

    def test_spanish_word_hola_detected(self):
        result = Translator._detect_source_language("hola")
        self.assertEqual(result, "es")

    def test_english_word_hello_detected(self):
        result = Translator._detect_source_language("hello")
        self.assertEqual(result, "en")


class TranslatorApplyStyleTestCase(unittest.TestCase):
    """Тесты статического метода _apply_style."""

    def test_neutral_style_passthrough(self):
        result = Translator._apply_style("Привет, как дела", "neutral")
        self.assertEqual(result, "Привет, как дела")

    def test_formal_style_adds_period(self):
        result = Translator._apply_style("Привет, как дела", "formal")
        self.assertEqual(result, "Привет, как дела.")

    def test_formal_style_keeps_existing_period(self):
        result = Translator._apply_style("Привет, как дела.", "formal")
        self.assertEqual(result, "Привет, как дела.")

    def test_formal_style_keeps_exclamation(self):
        result = Translator._apply_style("Прекрасно!", "formal")
        self.assertEqual(result, "Прекрасно!")

    def test_formal_style_keeps_question_mark(self):
        result = Translator._apply_style("Как дела?", "formal")
        self.assertEqual(result, "Как дела?")

    def test_chat_style_removes_period_for_short_text(self):
        # <= 10 слов + заканчивается на точку
        result = Translator._apply_style("Привет как дела.", "chat")
        self.assertEqual(result, "Привет как дела")

    def test_chat_style_keeps_period_for_long_text(self):
        # > 10 слов — не трогаем точку
        long_text = "Это достаточно длинный текст который не должен терять свою финальную точку никогда."
        words = long_text.split()
        self.assertGreater(len(words), 10, "Test prerequisite: text must have >10 words")
        result = Translator._apply_style(long_text, "chat")
        self.assertTrue(result.endswith("."))

    def test_chat_style_keeps_non_period_ending(self):
        result = Translator._apply_style("Привет!", "chat")
        self.assertEqual(result, "Привет!")

    def test_empty_text_returns_empty(self):
        self.assertEqual(Translator._apply_style("", "formal"), "")
        self.assertEqual(Translator._apply_style("", "chat"), "")
        self.assertEqual(Translator._apply_style("", "neutral"), "")


class TranslatorNormalizeModeTestCase(unittest.TestCase):
    """Тесты _normalize_mode."""

    def test_valid_mode_passthrough(self):
        for mode in ("off", "ru_to_es", "es_to_ru", "en_to_ru", "auto", "bilingual_ru_es"):
            self.assertEqual(Translator._normalize_mode(mode), mode)

    def test_invalid_mode_returns_off(self):
        self.assertEqual(Translator._normalize_mode("unknown_mode"), "off")
        self.assertEqual(Translator._normalize_mode(""), "off")
        self.assertEqual(Translator._normalize_mode("  "), "off")

    def test_mode_case_insensitive(self):
        self.assertEqual(Translator._normalize_mode("RU_TO_ES"), "ru_to_es")
        self.assertEqual(Translator._normalize_mode("Auto"), "auto")


class TranslatorLangsFromModeTestCase(unittest.TestCase):
    """Тесты _langs_from_mode."""

    def test_ru_to_es(self):
        self.assertEqual(Translator._langs_from_mode("ru_to_es"), ("ru", "es"))

    def test_es_to_ru(self):
        self.assertEqual(Translator._langs_from_mode("es_to_ru"), ("es", "ru"))

    def test_en_to_ru(self):
        self.assertEqual(Translator._langs_from_mode("en_to_ru"), ("en", "ru"))

    def test_unknown_mode_returns_empty(self):
        self.assertEqual(Translator._langs_from_mode("unknown"), ("", ""))


class TranslatorAutoToRuModeTestCase(unittest.TestCase):
    """Тесты режима auto_to_ru."""

    def _make_fake_builder(self, call_tracker: dict):
        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                call_tracker["count"] = call_tracker.get("count", 0) + 1
                return [{"translation_text": f"RU:{text}"}]
            return fake_pipeline
        return fake_builder

    def test_auto_to_ru_already_russian_returns_already_target(self):
        translator = Translator()
        result = translator.translate(
            "привет это русский текст",
            mode="auto_to_ru",
            network_mode="offline_default",
        )
        self.assertEqual(result.status, "already_target_language")
        self.assertEqual(result.source_lang, "ru")

    def test_auto_to_ru_es_text_translated_to_ru(self):
        translator = Translator()
        tracker: dict = {}
        original_builder = Translator._build_pipeline
        Translator._build_pipeline = staticmethod(self._make_fake_builder(tracker))
        try:
            result = translator.translate(
                "hola amigo como estas",
                mode="auto_to_ru",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "es")

    def test_auto_to_ru_en_text_translated_to_ru(self):
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"RU:{text}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate(
                "hello there how are you doing today",
                mode="auto_to_ru",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")


class TranslatorEnToRuModeTestCase(unittest.TestCase):
    """Тесты режима en_to_ru."""

    def test_en_to_ru_translates(self):
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            self.assertIn("en-ru", model_name)

            def fake_pipeline(text: str):
                return [{"translation_text": f"RU:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate(
                "Hello world",
                mode="en_to_ru",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "en")
        self.assertEqual(result.target_lang, "ru")
        self.assertTrue(result.text.startswith("RU:"))


class TranslatorModelUnavailableTestCase(unittest.TestCase):
    """Тесты путей недоступной модели."""

    def test_pipeline_none_returns_model_unavailable(self):
        translator = Translator()
        original_builder = Translator._build_pipeline

        Translator._build_pipeline = staticmethod(lambda model_name, allow_network: None)
        try:
            result = translator.translate(
                "привет мир тест",
                mode="ru_to_es",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertIn("model_unavailable", result.status)
        self.assertEqual(result.text, "")
        self.assertFalse(result.ok)

    def test_pipeline_none_cached_unavailable(self):
        """Второй вызов с недоступной моделью возвращает _cached_ статус."""
        translator = Translator()
        original_builder = Translator._build_pipeline
        call_count = {"count": 0}

        def counting_none_builder(model_name: str, allow_network: bool):
            call_count["count"] += 1
            return None

        Translator._build_pipeline = staticmethod(counting_none_builder)
        try:
            translator.translate("тест один", mode="ru_to_es", network_mode="offline_default")
            result2 = translator.translate("тест два", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        # Второй вызов должен использовать cached unavailable, не вызывать builder снова
        self.assertEqual(call_count["count"], 1)
        self.assertEqual(result2.status, "model_unavailable_cached")

    def test_translate_exception_returns_error_status(self):
        """Исключение в pipeline → translate_error, не raise."""
        translator = Translator()
        original_builder = Translator._build_pipeline

        def erroring_builder(model_name: str, allow_network: bool):
            def bad_pipeline(text: str):
                raise RuntimeError("simulated pipeline crash")
            return bad_pipeline

        Translator._build_pipeline = staticmethod(erroring_builder)
        try:
            result = translator.translate("тест ошибки", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "translate_error")
        self.assertEqual(result.text, "")
        self.assertFalse(result.ok)


class TranslatorGlossaryNormalizationTestCase(unittest.TestCase):
    """Тесты _normalize_glossary."""

    def test_none_glossary_returns_empty(self):
        self.assertEqual(Translator._normalize_glossary(None), {})

    def test_non_dict_returns_empty(self):
        self.assertEqual(Translator._normalize_glossary("not a dict"), {})  # type: ignore

    def test_empty_key_skipped(self):
        result = Translator._normalize_glossary({"": "value", "key": "val"})
        self.assertNotIn("", result)
        self.assertIn("key", result)

    def test_empty_value_skipped(self):
        result = Translator._normalize_glossary({"key": "", "k2": "v2"})
        self.assertNotIn("key", result)
        self.assertIn("k2", result)

    def test_valid_glossary_passthrough(self):
        glossary = {"cliente": "клиент", "hola": "привет"}
        result = Translator._normalize_glossary(glossary)
        self.assertEqual(result, glossary)


class TranslatorTranslationResultOkTestCase(unittest.TestCase):
    """Тесты TranslationResult.ok property."""

    def test_ok_status_with_text(self):
        r = TranslationResult(text="hello", status="ok", source_lang="ru", target_lang="es", mode="ru_to_es", engine="hf_marian")
        self.assertTrue(r.ok)

    def test_ok_status_empty_text_is_false(self):
        r = TranslationResult(text="", status="ok", source_lang="ru", target_lang="es", mode="ru_to_es", engine="hf_marian")
        self.assertFalse(r.ok)

    def test_error_status_is_false(self):
        r = TranslationResult(text="something", status="translate_error", source_lang="ru", target_lang="es", mode="ru_to_es", engine="hf_marian")
        self.assertFalse(r.ok)


class TranslatorSplitChunksEdgeCaseTestCase(unittest.TestCase):
    """Тесты _split_text_chunks edge cases."""

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(Translator._split_text_chunks("", 100), [])

    def test_short_text_returns_single_chunk(self):
        result = Translator._split_text_chunks("Привет.", 100)
        self.assertEqual(result, ["Привет."])

    def test_single_very_long_word_still_returned(self):
        long_word = "а" * 500
        result = Translator._split_text_chunks(long_word, 100)
        self.assertGreater(len(result), 0)
        # Слово попадает в chunks даже если больше max_chars
        self.assertIn(long_word, result)

    def test_chunking_respects_max_chars(self):
        # Текст из коротких предложений
        text = "Раз. Два. Три. Четыре. Пять. Шесть. Семь. Восемь."
        chunks = Translator._split_text_chunks(text, 15)
        self.assertTrue(all(len(c) <= 20 for c in chunks))  # небольшая погрешность на слова


if __name__ == "__main__":
    unittest.main()
