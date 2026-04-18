"""Unit-тесты для TranslationService.

Покрывает:
- handle_translate_text: базовый перевод RU→ES, auto-detect режим, error path
- handle_set_translation_glossary_item: добавление/обновление пары, персистенция
- handle_remove_translation_glossary_item: удаление пары, персистенция
- handle_get_glossary_suggestions: предложения из истории, фильтрация глоссария
- handle_get_vocabulary_suggestions: предложения слов из истории, stop-words
- edge cases: пустая строка, unicode/emoji, кэш глоссария, отсутствие history
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

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
    mode: str = "ru_es",
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
    history_items: list[dict] | None = None,
    vocabulary: list[str] | None = None,
    translator_side_effect: Exception | None = None,
) -> tuple[TranslationService, MagicMock, MagicMock]:
    """Создаёт TranslationService с полностью мокированными зависимостями."""
    effective_settings: dict[str, Any] = {
        "network_mode": "offline_default",
        "translation_glossary": {},
    }
    if settings:
        effective_settings.update(settings)

    translator = MagicMock()
    if translator_side_effect is not None:
        translator.translate.side_effect = translator_side_effect
    else:
        translator.translate.return_value = _make_result()

    store = MagicMock()
    store.get_history_page.return_value = (history_items or [], None)
    store.save_settings.side_effect = lambda s: s
    store.load_vocabulary.return_value = vocabulary or []

    settings_cache = [dict(effective_settings)]  # mutable cell

    def cached_settings() -> dict[str, Any]:
        return dict(settings_cache[0])

    invalidated = [False]

    def invalidate() -> None:
        invalidated[0] = True

    svc = TranslationService(
        translator=translator,
        store=store,
        cached_settings=cached_settings,
        invalidate_settings_cache=invalidate,
    )
    return svc, translator, store


# ──────────────────────────────────────────────────────────────
# Test cases
# ──────────────────────────────────────────────────────────────

class HandleTranslateTextTestCase(unittest.TestCase):
    """handle_translate_text — базовые сценарии."""

    # 1. basic RU→ES translation
    def test_basic_ru_es_translation(self) -> None:
        """RU→ES: результат содержит ожидаемые поля и вызов переводчика."""
        svc, translator, _ = _make_service()
        translator.translate.return_value = _make_result(
            text="Hola mundo",
            source_lang="ru",
            target_lang="es",
            mode="ru_es",
            engine="opus_mt",
        )

        result = svc.handle_translate_text({
            "text": "Привет мир",
            "translation_mode": "ru_es",
        })

        self.assertEqual(result["text"], "Hola mundo")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_lang"], "ru")
        self.assertEqual(result["target_lang"], "es")
        self.assertEqual(result["translation_mode"], "ru_es")
        self.assertEqual(result["engine"], "opus_mt")
        translator.translate.assert_called_once()
        call_kwargs = translator.translate.call_args
        self.assertEqual(call_kwargs.kwargs.get("text") or call_kwargs.args[0], "Привет мир")

    # 2. auto-detect language mode
    def test_auto_detect_mode(self) -> None:
        """auto режим передаётся переводчику как mode='auto'."""
        svc, translator, _ = _make_service()
        translator.translate.return_value = _make_result(mode="auto")

        result = svc.handle_translate_text({
            "text": "Hello world",
            "translation_mode": "auto",
        })

        self.assertEqual(result["translation_mode"], "auto")
        _, call_kwargs = translator.translate.call_args[0], translator.translate.call_args[1]
        # mode должен быть передан переводчику
        self.assertEqual(translator.translate.call_args.kwargs.get("mode") or
                         translator.translate.call_args[1].get("mode") or
                         translator.translate.call_args[0][1],
                         "auto")

    # 3. translator raises — exception propagates
    def test_translator_raises_propagates(self) -> None:
        """Если переводчик бросает исключение, оно должно пробрасываться выше."""
        svc, _, _ = _make_service(translator_side_effect=RuntimeError("model not loaded"))
        with self.assertRaises(RuntimeError, msg="model not loaded"):
            svc.handle_translate_text({"text": "тест", "translation_mode": "ru_es"})

    # 4. empty string input
    def test_empty_text_calls_translator(self) -> None:
        """Пустая строка передаётся переводчику (strip оставляет '')."""
        svc, translator, _ = _make_service()
        translator.translate.return_value = _make_result(text="", status="ok")

        result = svc.handle_translate_text({"text": "", "translation_mode": "ru_es"})
        self.assertEqual(result["text"], "")
        translator.translate.assert_called_once()

    # 5. unicode + emoji
    def test_unicode_emoji_handled(self) -> None:
        """Unicode и эмодзи сохраняются в результате без ошибок."""
        svc, translator, _ = _make_service()
        translated = "Hola 🌍 — привет"
        translator.translate.return_value = _make_result(text=translated)

        result = svc.handle_translate_text({
            "text": "Привет 🌍 — Hello",
            "translation_mode": "auto",
        })
        self.assertEqual(result["text"], translated)

    # 6. glossary from settings is passed to translator
    def test_glossary_passed_to_translator(self) -> None:
        """Глоссарий из настроек передаётся в translator.translate."""
        glossary = {"Krab": "Краб", "Ear": "Ухо"}
        svc, translator, _ = _make_service(settings={"translation_glossary": glossary})
        translator.translate.return_value = _make_result()

        svc.handle_translate_text({"text": "Krab Ear", "translation_mode": "ru_es"})

        call_glossary = translator.translate.call_args.kwargs.get("glossary")
        self.assertEqual(call_glossary, glossary)

    # 7. translation_style forwarded
    def test_translation_style_returned(self) -> None:
        """translation_style из params возвращается в ответе."""
        svc, _, _ = _make_service()

        result = svc.handle_translate_text({
            "text": "тест",
            "translation_mode": "ru_es",
            "translation_style": "formal",
        })
        self.assertEqual(result["translation_style"], "formal")


class HandleGlossaryTestCase(unittest.TestCase):
    """handle_set/remove_translation_glossary_item."""

    # 8. set glossary item — adds entry and persists
    def test_set_glossary_item_adds_entry(self) -> None:
        """Добавление пары: результат содержит updated=True и store.save_settings вызван."""
        svc, _, store = _make_service()

        result = svc.handle_set_translation_glossary_item({
            "source": "Краб",
            "target": "Krab",
        })

        self.assertTrue(result["updated"])
        store.save_settings.assert_called_once()
        saved = store.save_settings.call_args[0][0]
        self.assertEqual(saved["translation_glossary"]["Краб"], "Krab")

    # 9. set glossary — invalidates settings cache
    def test_set_glossary_invalidates_cache(self) -> None:
        """После сохранения глоссария кэш настроек инвалидируется."""
        invalidated_flag = [False]

        def invalidate():
            invalidated_flag[0] = True

        svc = TranslationService(
            translator=MagicMock(),
            store=MagicMock(save_settings=lambda s: s),
            cached_settings=lambda: {"translation_glossary": {}},
            invalidate_settings_cache=invalidate,
        )
        svc.handle_set_translation_glossary_item({"source": "X", "target": "Y"})
        self.assertTrue(invalidated_flag[0])

    # 10. set glossary — missing source raises RuntimeError
    def test_set_glossary_missing_source_raises(self) -> None:
        """Если source пустой — RuntimeError."""
        svc, _, _ = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_set_translation_glossary_item({"source": "", "target": "Krab"})

    # 11. set glossary — missing target raises RuntimeError
    def test_set_glossary_missing_target_raises(self) -> None:
        """Если target пустой — RuntimeError."""
        svc, _, _ = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_set_translation_glossary_item({"source": "Краб", "target": ""})

    # 12. remove glossary item — removes and persists
    def test_remove_glossary_item(self) -> None:
        """Удаление пары: removed=True и пара исчезает из сохранённых настроек."""
        existing = {"Краб": "Krab", "Ухо": "Ear"}
        svc, _, store = _make_service(settings={"translation_glossary": dict(existing)})

        result = svc.handle_remove_translation_glossary_item({"source": "Краб"})

        self.assertTrue(result["removed"])
        store.save_settings.assert_called_once()
        saved = store.save_settings.call_args[0][0]
        self.assertNotIn("Краб", saved["translation_glossary"])
        self.assertIn("Ухо", saved["translation_glossary"])

    # 13. remove glossary — missing source raises RuntimeError
    def test_remove_glossary_missing_source_raises(self) -> None:
        """Если source пустой — RuntimeError."""
        svc, _, _ = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_remove_translation_glossary_item({"source": ""})

    # 14. remove non-existent key — no error, count unchanged
    def test_remove_nonexistent_key_no_error(self) -> None:
        """Удаление несуществующего ключа не вызывает ошибку."""
        svc, _, store = _make_service(settings={"translation_glossary": {"A": "B"}})
        result = svc.handle_remove_translation_glossary_item({"source": "NonExistent"})
        self.assertTrue(result["removed"])
        # count should still be 1 (only "A":"B" remains)
        self.assertEqual(result["count"], 1)


class HandleGetGlossarySuggestionsTestCase(unittest.TestCase):
    """handle_get_glossary_suggestions."""

    # 15. returns suggestions from history with capitalized words
    def test_suggestions_from_history_pairs(self) -> None:
        """Заглавные слова в парах source→translated предлагаются как кандидаты."""
        history = [
            {"source_text": "Google запустил новый продукт", "translated_text": "Google lanzó un nuevo producto"},
            {"source_text": "Google снова обновился", "translated_text": "Google se actualiza de nuevo"},
        ]
        svc, _, _ = _make_service(history_items=history)
        result = svc.handle_get_glossary_suggestions({"scan_limit": 50, "min_count": 2, "top_k": 10})

        self.assertIn("suggestions", result)
        self.assertIn("scanned_items", result)
        self.assertEqual(result["scanned_items"], 2)
        # Google появляется в обоих текстах, должен быть в suggestions
        sources = [s["source"] for s in result["suggestions"]]
        self.assertIn("Google", sources)

    # 16. already-in-glossary items are filtered out
    def test_already_in_glossary_filtered(self) -> None:
        """Слова уже в глоссарии не предлагаются снова."""
        history = [
            {"source_text": "Apple вышла", "translated_text": "Apple salió"},
            {"source_text": "Apple растёт", "translated_text": "Apple crece"},
        ]
        svc, _, _ = _make_service(
            settings={"translation_glossary": {"Apple": "Apple"}},
            history_items=history,
        )
        result = svc.handle_get_glossary_suggestions({"min_count": 2})
        sources = [s["source"] for s in result["suggestions"]]
        self.assertNotIn("Apple", sources)

    # 17. empty history — returns empty suggestions
    def test_empty_history_returns_empty_or_brands(self) -> None:
        """Пустая история: suggestions только из brand_replacements."""
        svc, _, _ = _make_service(history_items=[])
        result = svc.handle_get_glossary_suggestions({})
        self.assertIn("suggestions", result)
        self.assertEqual(result["scanned_items"], 0)
        # brand_replacement suggestions may exist; but no history_pair ones
        for s in result["suggestions"]:
            self.assertNotEqual(s["origin"], "history_pair")

    # 18. response structure is complete
    def test_response_structure(self) -> None:
        """Ответ содержит все обязательные поля."""
        svc, _, _ = _make_service()
        result = svc.handle_get_glossary_suggestions({})
        for key in ("suggestions", "total_candidates", "scanned_items", "current_glossary_size"):
            self.assertIn(key, result)


class HandleGetVocabularySuggestionsTestCase(unittest.TestCase):
    """handle_get_vocabulary_suggestions."""

    # 19. frequently occurring words are suggested
    def test_frequent_words_suggested(self) -> None:
        """Слова с freq >= min_count попадают в suggestions."""
        # "транскрибация" встречается 3 раза, должна попасть
        history = [
            {"text": "транскрибация началась", "source_text": ""},
            {"text": "транскрибация завершена", "source_text": ""},
            {"text": "транскрибация запущена", "source_text": ""},
        ]
        svc, _, _ = _make_service(history_items=history)
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 4})

        words = [s["word"] for s in result["suggestions"]]
        self.assertIn("транскрибация", words)

    # 20. stop words are excluded
    def test_stop_words_excluded(self) -> None:
        """Стоп-слова не попадают в suggestions."""
        # "нужно" — стоп-слово, встречается много раз
        history = [{"text": "нужно нужно нужно нужно нужно", "source_text": ""}]
        svc, _, _ = _make_service(history_items=history)
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 2})

        words = [s["word"] for s in result["suggestions"]]
        self.assertNotIn("нужно", words)

    # 21. words already in vocabulary are excluded
    def test_existing_vocabulary_excluded(self) -> None:
        """Слова уже в vocabulary не предлагаются."""
        history = [
            {"text": "микрофон микрофон микрофон", "source_text": ""},
        ]
        svc, _, store = _make_service(
            history_items=history,
            vocabulary=["микрофон"],
        )
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 4})

        words = [s["word"] for s in result["suggestions"]]
        self.assertNotIn("микрофон", words)

    # 22. response structure is complete
    def test_response_structure(self) -> None:
        """Ответ содержит все обязательные поля."""
        svc, _, _ = _make_service()
        result = svc.handle_get_vocabulary_suggestions({})
        for key in ("suggestions", "total_candidates", "scanned_items", "current_vocabulary_size"):
            self.assertIn(key, result)

    # 23. source_text preferred over text field
    def test_source_text_preferred_over_text(self) -> None:
        """source_text используется вместо text когда оба присутствуют."""
        # source_text содержит "серверный", text — нет
        history = [
            {"text": "translated version", "source_text": "серверный серверный серверный"},
        ]
        svc, _, _ = _make_service(history_items=history)
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 4})

        words = [s["word"] for s in result["suggestions"]]
        self.assertIn("серверный", words)
        self.assertNotIn("translated", words)

    # 24. empty history returns empty suggestions
    def test_empty_history_empty_suggestions(self) -> None:
        """Пустая история — пустой список suggestions."""
        svc, _, _ = _make_service(history_items=[])
        result = svc.handle_get_vocabulary_suggestions({})
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["scanned_items"], 0)


if __name__ == "__main__":
    unittest.main()
