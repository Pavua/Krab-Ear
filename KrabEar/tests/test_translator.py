"""Unit-тесты offline-first переводчика Krab Ear."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator


class TranslatorTestCase(unittest.TestCase):
    """Проверяет маршрутизацию режимов и офлайн-политику."""

    def test_off_mode_skips_translation(self) -> None:
        translator = Translator()
        result = translator.translate("Привет", mode="off", network_mode="offline_default")
        self.assertEqual(result.status, "not_requested")
        self.assertEqual(result.text, "")

    def test_offline_default_passes_local_only_flag(self) -> None:
        translator = Translator()
        calls: list[tuple[str, bool]] = []

        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            calls.append((model_name, allow_network))

            def fake_pipeline(text: str):
                return [{"translation_text": f"FAKE:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate("тест", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "FAKE:тест")
        self.assertEqual(result.mode, "ru_to_es")
        self.assertTrue(calls)
        self.assertFalse(calls[0][1])

    def test_online_opt_in_allows_network(self) -> None:
        translator = Translator()
        calls: list[tuple[str, bool]] = []

        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            calls.append((model_name, allow_network))

            def fake_pipeline(text: str):
                return [{"translation_text": f"FAKE:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate("hola amigo", mode="es_to_ru", network_mode="online_opt_in")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.mode, "es_to_ru")
        self.assertTrue(calls)
        self.assertTrue(calls[0][1])

    def test_split_text_chunks_for_long_text(self) -> None:
        long_text = (
            "Это очень длинный абзац без потери смысла. "
            "Мы хотим убедиться, что переводчик разделяет текст на части. "
            "Каждая часть должна быть не слишком длинной для модели. "
            "При этом порядок и итоговая склейка должны оставаться корректными."
        ) * 6
        chunks = Translator._split_text_chunks(long_text, max_chars=180)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 180 for chunk in chunks))

    def test_translate_long_text_uses_multiple_chunks(self) -> None:
        translator = Translator()
        call_counter = {"count": 0}

        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                call_counter["count"] += 1
                return [{"translation_text": f"TR:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            long_text = ("Привет. " * 250).strip()
            result = translator.translate(long_text, mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.text.startswith("TR:"))
        self.assertGreater(call_counter["count"], 1)

    def test_translation_style_and_glossary(self) -> None:
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"cliente. {text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result_chat = translator.translate(
                "texto base",
                mode="es_to_ru",
                network_mode="offline_default",
                translation_style="chat",
                glossary={"cliente": "клиент"},
            )
            result_formal = translator.translate(
                "texto base dos",
                mode="es_to_ru",
                network_mode="offline_default",
                translation_style="formal",
                glossary={"cliente": "клиент"},
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result_chat.status, "ok")
        self.assertIn("клиент", result_chat.text)
        self.assertFalse(result_chat.text.endswith(".."))
        self.assertEqual(result_formal.status, "ok")
        self.assertTrue(result_formal.text.endswith("."))

    def test_translation_cache_reuses_pipeline_result(self) -> None:
        translator = Translator()
        calls = {"count": 0}
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                calls["count"] += 1
                return [{"translation_text": f"TR:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            first = translator.translate("один и тот же текст", mode="ru_to_es", network_mode="offline_default")
            second = translator.translate("один и тот же текст", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "ok")
        self.assertEqual(calls["count"], 1)

    def test_bilingual_mode_ru_to_es(self) -> None:
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"ES:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate(
                "привет мир",
                mode="bilingual_ru_es",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.mode, "bilingual_ru_es")
        self.assertIn("RU: привет мир", result.text)
        self.assertIn("ES: ES:привет мир", result.text)

    def test_bilingual_mode_es_to_ru(self) -> None:
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"RU:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate(
                "hola gracias",
                mode="bilingual_ru_es",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertIn("ES: hola gracias", result.text)
        self.assertIn("RU: RU:hola gracias", result.text)

    def test_cache_separates_network_modes(self) -> None:
        translator = Translator()
        calls = {"count": 0}
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                calls["count"] += 1
                return [{"translation_text": f"{'ON' if allow_network else 'OFF'}:{text}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            offline = translator.translate("hola", mode="es_to_ru", network_mode="offline_default")
            online = translator.translate("hola", mode="es_to_ru", network_mode="online_opt_in")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(offline.status, "ok")
        self.assertEqual(online.status, "ok")
        self.assertNotEqual(offline.text, online.text)
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
