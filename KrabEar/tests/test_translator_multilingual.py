"""Тесты новых языковых пар (Phase D.3): en→es, es→en, ru→en, de→en, en→de.

Все тесты используют моки — NLLB и Marian модели не загружаются.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator, TranslationResult  # noqa: E402


def _make_fake_pipeline(output_text: str):
    """Создаёт mock pipeline, возвращающий заданный перевод."""
    fake = MagicMock()
    fake.return_value = [{"translation_text": output_text}]
    return fake


class TestNewLanguagePairsRouting(unittest.TestCase):
    """Проверяет что новые mode строки маршрутизируются в правильные модели."""

    def _make_translator_with_fake_pipeline(self, mode: str, output_text: str):
        """Возвращает Translator, у которого _build_pipeline всегда даёт fake pipeline."""
        translator = Translator()
        fake_pipeline = _make_fake_pipeline(output_text)

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            return fake_pipeline

        translator._build_pipeline = staticmethod(fake_build)
        return translator, fake_pipeline

    # ---------- en→es ----------

    def test_en_es_routes_to_correct_model(self) -> None:
        """en_to_es должен использовать Helsinki-NLP/opus-mt-en-es."""
        translator = Translator()
        captured_models: list[str] = []

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            captured_models.append(model_name)
            return _make_fake_pipeline("Hola mundo")

        translator._build_pipeline = staticmethod(fake_build)
        result = translator.translate("Hello world", mode="en_to_es", network_mode="offline_default")

        self.assertIn("opus-mt-en-es", captured_models[0])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "en")
        self.assertEqual(result.target_lang, "es")
        self.assertEqual(result.text, "Hola mundo")

    def test_en_es_supported_mode(self) -> None:
        """en_to_es должен быть в _SUPPORTED_MODES."""
        self.assertIn("en_to_es", Translator._SUPPORTED_MODES)

    # ---------- es→en ----------

    def test_es_en_routes_to_correct_model(self) -> None:
        """es_to_en должен использовать Helsinki-NLP/opus-mt-es-en."""
        translator = Translator()
        captured_models: list[str] = []

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            captured_models.append(model_name)
            return _make_fake_pipeline("Hello world")

        translator._build_pipeline = staticmethod(fake_build)
        result = translator.translate("Hola mundo", mode="es_to_en", network_mode="offline_default")

        self.assertIn("opus-mt-es-en", captured_models[0])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "es")
        self.assertEqual(result.target_lang, "en")

    def test_es_en_supported_mode(self) -> None:
        self.assertIn("es_to_en", Translator._SUPPORTED_MODES)

    # ---------- ru→en ----------

    def test_ru_en_routes_to_correct_model(self) -> None:
        """ru_to_en должен использовать Helsinki-NLP/opus-mt-ru-en."""
        translator = Translator()
        captured_models: list[str] = []

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            captured_models.append(model_name)
            return _make_fake_pipeline("Hello Russia")

        translator._build_pipeline = staticmethod(fake_build)
        result = translator.translate("Привет Россия", mode="ru_to_en", network_mode="offline_default")

        self.assertIn("opus-mt-ru-en", captured_models[0])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "ru")
        self.assertEqual(result.target_lang, "en")

    def test_ru_en_supported_mode(self) -> None:
        self.assertIn("ru_to_en", Translator._SUPPORTED_MODES)

    # ---------- de→en ----------

    def test_de_en_routes_to_correct_model(self) -> None:
        """de_to_en должен использовать Helsinki-NLP/opus-mt-de-en."""
        translator = Translator()
        captured_models: list[str] = []

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            captured_models.append(model_name)
            return _make_fake_pipeline("Hello Germany")

        translator._build_pipeline = staticmethod(fake_build)
        result = translator.translate("Hallo Deutschland", mode="de_to_en", network_mode="offline_default")

        self.assertIn("opus-mt-de-en", captured_models[0])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "de")
        self.assertEqual(result.target_lang, "en")

    def test_de_en_supported_mode(self) -> None:
        self.assertIn("de_to_en", Translator._SUPPORTED_MODES)

    # ---------- en→de ----------

    def test_en_de_routes_to_correct_model(self) -> None:
        """en_to_de должен использовать Helsinki-NLP/opus-mt-en-de."""
        translator = Translator()
        captured_models: list[str] = []

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            captured_models.append(model_name)
            return _make_fake_pipeline("Hallo Welt")

        translator._build_pipeline = staticmethod(fake_build)
        result = translator.translate("Hello world", mode="en_to_de", network_mode="offline_default")

        self.assertIn("opus-mt-en-de", captured_models[0])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source_lang, "en")
        self.assertEqual(result.target_lang, "de")

    def test_en_de_supported_mode(self) -> None:
        self.assertIn("en_to_de", Translator._SUPPORTED_MODES)

    # ---------- unknown mode ----------

    def test_unknown_mode_returns_off_status(self) -> None:
        """Неизвестный mode нормализуется в 'off' → статус not_requested."""
        translator = Translator()
        result = translator.translate("some text", mode="klingon_to_elvish", network_mode="offline_default")
        # _normalize_mode превращает неизвестный mode в "off" → not_requested
        self.assertEqual(result.status, "not_requested")
        self.assertEqual(result.text, "")

    # ---------- NLLB-200 fallback ----------

    def test_nllb_fallback_used_when_marian_unavailable(self) -> None:
        """Когда Marian модель недоступна, должен быть использован NLLB-200."""
        nllb_pipeline = _make_fake_pipeline("Hola mundo")

        with (
            patch.object(Translator, "_build_pipeline", staticmethod(lambda model_name, allow_network: None)),
            patch.object(Translator, "_build_nllb_pipeline", classmethod(
                lambda cls, src_lang, tgt_lang, allow_network: nllb_pipeline
            )),
        ):
            translator = Translator()
            result = translator.translate("Hello world", mode="en_to_es", network_mode="offline_default")

        self.assertEqual(result.engine, "nllb200")
        self.assertEqual(result.status, "ok")
        self.assertNotEqual(result.text, "")

    def test_nllb_fallback_unavailable_returns_model_unavailable(self) -> None:
        """Когда и Marian и NLLB недоступны, возвращаем model_unavailable_offline."""
        with (
            patch.object(Translator, "_build_pipeline", staticmethod(lambda model_name, allow_network: None)),
            patch.object(Translator, "_build_nllb_pipeline", classmethod(
                lambda cls, src_lang, tgt_lang, allow_network: None
            )),
        ):
            translator = Translator()
            result = translator.translate("Hello world", mode="en_to_es", network_mode="offline_default")

        self.assertEqual(result.status, "model_unavailable_offline")
        self.assertEqual(result.text, "")


class TestLanguageDetectionGerman(unittest.TestCase):
    """Проверяет определение немецкого языка в auto режиме."""

    def test_detect_german_umlauts(self) -> None:
        """Текст с умлаутами должен определяться как немецкий."""
        detected = Translator._detect_source_language("Ich möchte gerne Österreich besuchen")
        self.assertEqual(detected, "de")

    def test_detect_german_markers(self) -> None:
        """Немецкие служебные слова должны определять язык как немецкий."""
        detected = Translator._detect_source_language("Ich und du sind nicht allein")
        self.assertEqual(detected, "de")

    def test_auto_mode_german_routes_to_de_en(self) -> None:
        """В режиме auto немецкий текст должен маршрутизироваться в de_to_en."""
        translator = Translator()
        captured_models: list[str] = []

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            captured_models.append(model_name)
            return _make_fake_pipeline("I want to visit Austria")

        translator._build_pipeline = staticmethod(fake_build)
        result = translator.translate(
            "Ich möchte gerne Österreich besuchen", mode="auto", network_mode="offline_default"
        )
        self.assertTrue(len(captured_models) > 0)
        self.assertIn("de-en", captured_models[0])
        self.assertEqual(result.source_lang, "de")


class TestLangsFromMode(unittest.TestCase):
    """Проверяет что _langs_from_mode корректно возвращает пары для новых режимов."""

    def test_en_es(self) -> None:
        self.assertEqual(Translator._langs_from_mode("en_to_es"), ("en", "es"))

    def test_es_en(self) -> None:
        self.assertEqual(Translator._langs_from_mode("es_to_en"), ("es", "en"))

    def test_ru_en(self) -> None:
        self.assertEqual(Translator._langs_from_mode("ru_to_en"), ("ru", "en"))

    def test_de_en(self) -> None:
        self.assertEqual(Translator._langs_from_mode("de_to_en"), ("de", "en"))

    def test_en_de(self) -> None:
        self.assertEqual(Translator._langs_from_mode("en_to_de"), ("en", "de"))

    def test_unknown_returns_empty(self) -> None:
        self.assertEqual(Translator._langs_from_mode("klingon_to_elvish"), ("", ""))


class TestModelByModeMapping(unittest.TestCase):
    """Проверяет что _MODEL_BY_MODE содержит все новые пары."""

    def test_en_es_model(self) -> None:
        self.assertIn("en_to_es", Translator._MODEL_BY_MODE)
        self.assertIn("opus-mt-en-es", Translator._MODEL_BY_MODE["en_to_es"])

    def test_es_en_model(self) -> None:
        self.assertIn("es_to_en", Translator._MODEL_BY_MODE)
        self.assertIn("opus-mt-es-en", Translator._MODEL_BY_MODE["es_to_en"])

    def test_ru_en_model(self) -> None:
        self.assertIn("ru_to_en", Translator._MODEL_BY_MODE)
        self.assertIn("opus-mt-ru-en", Translator._MODEL_BY_MODE["ru_to_en"])

    def test_de_en_model(self) -> None:
        self.assertIn("de_to_en", Translator._MODEL_BY_MODE)
        self.assertIn("opus-mt-de-en", Translator._MODEL_BY_MODE["de_to_en"])

    def test_en_de_model(self) -> None:
        self.assertIn("en_to_de", Translator._MODEL_BY_MODE)
        self.assertIn("opus-mt-en-de", Translator._MODEL_BY_MODE["en_to_de"])

    def test_nllb_model_defined(self) -> None:
        self.assertIn("nllb-200", Translator._NLLB_MODEL)

    def test_nllb_lang_map_covers_all_new_langs(self) -> None:
        for lang in ("ru", "es", "en", "de"):
            self.assertIn(lang, Translator._NLLB_LANG_MAP, f"Язык {lang} отсутствует в _NLLB_LANG_MAP")


class TestCachingNewModes(unittest.TestCase):
    """Проверяет кэширование для новых языковых пар."""

    def test_result_cached_after_first_call(self) -> None:
        """Повторный вызов с теми же параметрами не должен создавать pipeline."""
        translator = Translator()
        build_count = [0]

        def fake_build(model_name: str, allow_network: bool):  # noqa: ANN001
            build_count[0] += 1
            return _make_fake_pipeline("Hola mundo")

        translator._build_pipeline = staticmethod(fake_build)

        translator.translate("Hello world", mode="en_to_es", network_mode="offline_default")
        translator.translate("Hello world", mode="en_to_es", network_mode="offline_default")

        # Pipeline должен быть создан только один раз.
        self.assertEqual(build_count[0], 1)


if __name__ == "__main__":
    unittest.main()
