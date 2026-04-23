"""Unit-тесты для TranslationService.handle_translate_selection.

Покрывает:
- RU текст без source_lang → auto-detect → режим ru_to_es, target es
- ES текст без source_lang → auto-detect → режим es_to_ru, target ru
- EN текст без source_lang → auto-detect → режим en_to_ru, target ru
- Явные source_lang/target_lang → соблюдаются
- Пустой текст → пустой результат, нет ошибки
- Неверный lang-код → graceful fallback
- Глоссарий из settings применяется (передаётся translator.translate)
- latency_ms присутствует в ответе
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_service import TranslationService
from backend.translator import TranslationResult


# ──────────────────────────────────────────────────────────────
# Helpers / fakes
# ──────────────────────────────────────────────────────────────

def _make_result(
    text: str = "translated",
    status: str = "ok",
    source_lang: str = "ru",
    target_lang: str = "es",
    mode: str = "ru_to_es",
    engine: str = "opus_mt",
) -> TranslationResult:
    return TranslationResult(
        text=text,
        status=status,
        source_lang=source_lang,
        target_lang=target_lang,
        mode=mode,
        engine=engine,
    )


def _make_service(
    settings: dict[str, Any] | None = None,
) -> tuple[TranslationService, MagicMock]:
    """Создаёт TranslationService с мокированными зависимостями."""
    effective_settings: dict[str, Any] = {
        "network_mode": "offline_default",
        "translation_style": "neutral",
        "translation_glossary": {},
    }
    if settings:
        effective_settings.update(settings)

    translator = MagicMock()
    translator.translate.return_value = _make_result()

    store = MagicMock()
    store.get_history_page.return_value = ([], None)
    store.save_settings.side_effect = lambda s: s
    store.load_vocabulary.return_value = []

    svc = TranslationService(
        translator=translator,
        store=store,
        cached_settings=lambda: dict(effective_settings),
        invalidate_settings_cache=lambda: None,
    )
    return svc, translator


# ──────────────────────────────────────────────────────────────
# Test cases
# ──────────────────────────────────────────────────────────────

class TranslateSelectionAutoDetectTestCase(unittest.TestCase):
    """Auto-detect language direction tests."""

    def test_ru_text_auto_detects_to_es(self) -> None:
        """RU текст без source_lang → направление ru_to_es, target_lang=es."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(
            text="Hola mundo", source_lang="ru", target_lang="es", mode="ru_to_es",
        )
        result = svc.handle_translate_selection({"text": "Привет мир"})

        self.assertEqual(result["source_lang_detected"], "ru")
        self.assertEqual(result["target_lang"], "es")
        self.assertEqual(result["translated_text"], "Hola mundo")
        translator.translate.assert_called_once()
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs["mode"], "ru_to_es")

    def test_es_text_auto_detects_to_ru(self) -> None:
        """ES текст без source_lang → направление es_to_ru, target_lang=ru."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(
            text="Привет мир", source_lang="es", target_lang="ru", mode="es_to_ru",
        )
        result = svc.handle_translate_selection({"text": "Hola señor"})

        self.assertEqual(result["source_lang_detected"], "es")
        self.assertEqual(result["target_lang"], "ru")
        translator.translate.assert_called_once()
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs["mode"], "es_to_ru")

    def test_en_text_auto_detects_to_ru(self) -> None:
        """EN текст без source_lang → направление en_to_ru, target_lang=ru."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(
            text="Привет мир", source_lang="en", target_lang="ru", mode="en_to_ru",
        )
        result = svc.handle_translate_selection(
            {"text": "Hello world this is English"}
        )

        self.assertEqual(result["source_lang_detected"], "en")
        self.assertEqual(result["target_lang"], "ru")
        translator.translate.assert_called_once()
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs["mode"], "en_to_ru")


class TranslateSelectionExplicitLangTestCase(unittest.TestCase):
    """Явные source_lang / target_lang."""

    def test_explicit_source_and_target_respected(self) -> None:
        """Явные source_lang=ru, target_lang=es → используются как есть."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(
            text="Hola", source_lang="ru", target_lang="es", mode="ru_to_es",
        )
        result = svc.handle_translate_selection(
            {"text": "Привет", "source_lang": "ru", "target_lang": "es"}
        )
        self.assertEqual(result["source_lang_detected"], "ru")
        self.assertEqual(result["target_lang"], "es")
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs["mode"], "ru_to_es")

    def test_explicit_source_es_to_ru(self) -> None:
        """source_lang=es, target_lang=ru → режим es_to_ru."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(
            text="Привет", source_lang="es", target_lang="ru", mode="es_to_ru",
        )
        result = svc.handle_translate_selection(
            {"text": "Hola mundo", "source_lang": "es", "target_lang": "ru"}
        )
        self.assertEqual(result["source_lang_detected"], "es")
        self.assertEqual(result["target_lang"], "ru")
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs["mode"], "es_to_ru")

    def test_explicit_source_only_infers_target(self) -> None:
        """Только source_lang=ru без target_lang → target_lang=es (default map)."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(text="hola")
        result = svc.handle_translate_selection(
            {"text": "Привет", "source_lang": "ru"}
        )
        self.assertEqual(result["source_lang_detected"], "ru")
        self.assertEqual(result["target_lang"], "es")


class TranslateSelectionEdgeCasesTestCase(unittest.TestCase):
    """Edge cases: empty, invalid lang, glossary."""

    def test_empty_text_returns_empty_no_error(self) -> None:
        """Пустой text → пустой ответ, translator не вызывается."""
        svc, translator = _make_service()
        result = svc.handle_translate_selection({"text": ""})

        self.assertEqual(result["translated_text"], "")
        self.assertEqual(result["source_lang_detected"], "")
        self.assertEqual(result["target_lang"], "")
        self.assertEqual(result["engine"], "none")
        self.assertEqual(result["latency_ms"], 0)
        translator.translate.assert_not_called()

    def test_whitespace_only_text_returns_empty(self) -> None:
        """Текст из пробелов → пустой ответ."""
        svc, translator = _make_service()
        result = svc.handle_translate_selection({"text": "   \t  "})
        self.assertEqual(result["translated_text"], "")
        translator.translate.assert_not_called()

    def test_invalid_lang_code_graceful_fallback(self) -> None:
        """Неверный source_lang → fallback на ru_to_es."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(text="fallback result")
        result = svc.handle_translate_selection(
            {"text": "some text", "source_lang": "zz"}
        )
        # Не должно бросать исключений; translator должен быть вызван
        self.assertIn("translated_text", result)
        self.assertIn("latency_ms", result)
        translator.translate.assert_called_once()

    def test_glossary_passed_to_translator(self) -> None:
        """Глоссарий из settings передаётся в translator.translate."""
        glossary = {"Краб": "Crab", "Ухо": "Ear"}
        svc, translator = _make_service(settings={"translation_glossary": glossary})
        translator.translate.return_value = _make_result(text="translated")
        svc.handle_translate_selection({"text": "Краб Ухо"})

        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs["glossary"], glossary)

    def test_latency_ms_present_and_non_negative(self) -> None:
        """latency_ms присутствует в ответе и >= 0."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(text="result")
        result = svc.handle_translate_selection({"text": "Привет"})
        self.assertIn("latency_ms", result)
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_network_mode_from_settings(self) -> None:
        """network_mode из settings передаётся в translator.translate."""
        svc, translator = _make_service(
            settings={"network_mode": "online_preferred"}
        )
        translator.translate.return_value = _make_result(text="ok")
        svc.handle_translate_selection({"text": "Привет мир"})

        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs["network_mode"], "online_preferred")

    def test_result_contains_all_required_fields(self) -> None:
        """Ответ всегда содержит все обязательные поля."""
        svc, translator = _make_service()
        translator.translate.return_value = _make_result(
            text="translated", engine="opus_mt"
        )
        result = svc.handle_translate_selection({"text": "Привет"})
        for field in ("translated_text", "source_lang_detected",
                      "target_lang", "engine", "latency_ms"):
            self.assertIn(field, result, f"missing field: {field}")


if __name__ == "__main__":
    unittest.main()
