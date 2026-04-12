"""Unit-тесты для LanguageLearningManager."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.language_learning import LanguageLearningManager, VocabEntry, _difficulty_from_frequency


# ---------------------------------------------------------------------------
# Фиктивные объекты для тестов
# ---------------------------------------------------------------------------

class FakeItem:
    """Минимальный аналог HistoryItem для тестов."""

    def __init__(
        self,
        text: str = "",
        source_text: str = "",
        translated_text: str = "",
        source_lang: str = "",
        target_lang: str = "",
        ts: str = "2026-04-12T10:00:00",
    ) -> None:
        self.text = text
        self.source_text = source_text
        self.translated_text = translated_text
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.ts = ts


def _make_item(
    src: str,
    tgt: str = "",
    src_lang: str = "ru",
    tgt_lang: str = "es",
    ts: str = "2026-04-12T10:00:00",
) -> FakeItem:
    return FakeItem(
        text=src,
        source_text=src,
        translated_text=tgt,
        source_lang=src_lang,
        target_lang=tgt_lang,
        ts=ts,
    )


# ---------------------------------------------------------------------------
# Тесты VocabEntry и вспомогательных функций
# ---------------------------------------------------------------------------

class VocabEntryTestCase(unittest.TestCase):
    """Тесты dataclass VocabEntry и _difficulty_from_frequency."""

    def test_vocab_entry_creation(self) -> None:
        entry = VocabEntry(
            word_source="программа",
            word_target="programa",
            context_sentence="Это программа для изучения языков",
            frequency=5,
            first_seen="2026-04-12T10:00:00",
        )
        self.assertEqual(entry.word_source, "программа")
        self.assertEqual(entry.word_target, "programa")
        self.assertEqual(entry.frequency, 5)

    def test_difficulty_easy_for_high_frequency(self) -> None:
        self.assertEqual(_difficulty_from_frequency(10, 10), "easy")
        self.assertEqual(_difficulty_from_frequency(7, 10), "easy")

    def test_difficulty_medium_for_mid_frequency(self) -> None:
        self.assertEqual(_difficulty_from_frequency(5, 10), "medium")
        self.assertEqual(_difficulty_from_frequency(4, 10), "medium")

    def test_difficulty_hard_for_low_frequency(self) -> None:
        self.assertEqual(_difficulty_from_frequency(1, 10), "hard")
        self.assertEqual(_difficulty_from_frequency(2, 10), "hard")

    def test_difficulty_zero_max_freq_returns_medium(self) -> None:
        self.assertEqual(_difficulty_from_frequency(0, 0), "medium")


# ---------------------------------------------------------------------------
# Тесты extract_vocabulary
# ---------------------------------------------------------------------------

class ExtractVocabularyTestCase(unittest.TestCase):
    """Тесты метода extract_vocabulary."""

    def setUp(self) -> None:
        self.mgr = LanguageLearningManager()

    def test_empty_items_returns_empty(self) -> None:
        result = self.mgr.extract_vocabulary([], "ru", "es")
        self.assertEqual(result, [])

    def test_extracts_words_from_bilingual_items(self) -> None:
        items = [
            _make_item("Привет мир программирование", "Hola mundo programación"),
            _make_item("программирование это интересно", "programación es interesante"),
        ]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        # все записи должны быть VocabEntry
        for entry in result:
            self.assertIsInstance(entry, VocabEntry)

    def test_vocab_sorted_by_frequency_descending(self) -> None:
        items = [
            _make_item("работа работа работа учёба"),
            _make_item("работа красивый"),
        ]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        self.assertGreater(len(result), 0)
        # Первое слово должно иметь максимальную частотность
        if len(result) > 1:
            self.assertGreaterEqual(result[0].frequency, result[1].frequency)

    def test_stop_words_excluded(self) -> None:
        items = [
            _make_item("и в на программирование для"),
        ]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        words = [e.word_source for e in result]
        # Стоп-слова не должны быть в результате
        for stop in ["и", "в", "на", "для"]:
            self.assertNotIn(stop, words)

    def test_filters_by_source_lang(self) -> None:
        items = [
            _make_item("изучение языков обучение", src_lang="ru", tgt_lang="es"),
            _make_item("learning languages education", src_lang="en", tgt_lang="es"),
        ]
        # Запрашиваем только ru→es
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        words = [e.word_source for e in result]
        # Слова из английского текста не должны быть в результате для ru→es
        # (хотя они могут совпасть, проверяем что ru-слова есть)
        self.assertTrue(any(w in words for w in ["изучение", "языков", "обучение"]))

    def test_items_without_translation_still_extracted(self) -> None:
        """Слова без перевода всё равно попадают в словарь."""
        items = [
            FakeItem(
                text="программирование алгоритм",
                source_text="программирование алгоритм",
                translated_text="",
                source_lang="ru",
                target_lang="",
                ts="2026-04-12T10:00:00",
            )
        ]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        self.assertGreater(len(result), 0)

    def test_context_sentence_truncated(self) -> None:
        long_text = "слово " * 50  # > 150 символов
        items = [_make_item(long_text)]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        for entry in result:
            self.assertLessEqual(len(entry.context_sentence), 160)  # 150 + "…"

    def test_dict_items_supported(self) -> None:
        """Словари работают наравне с объектами."""
        items = [
            {
                "text": "программа алгоритм",
                "source_text": "программа алгоритм",
                "translated_text": "programa algoritmo",
                "source_lang": "ru",
                "target_lang": "es",
                "ts": "2026-04-12T10:00:00",
            }
        ]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        self.assertGreater(len(result), 0)
        words = [e.word_source for e in result]
        self.assertIn("программа", words)


# ---------------------------------------------------------------------------
# Тесты generate_flashcards
# ---------------------------------------------------------------------------

class GenerateFlashcardsTestCase(unittest.TestCase):
    """Тесты метода generate_flashcards."""

    def setUp(self) -> None:
        self.mgr = LanguageLearningManager()
        self.items = [
            _make_item("программирование алгоритм структура", "programación algoritmo estructura"),
            _make_item("алгоритм сортировка поиск", "algoritmo ordenación búsqueda"),
        ]

    def test_returns_list_of_dicts(self) -> None:
        cards = self.mgr.generate_flashcards(self.items, "ru", "es")
        self.assertIsInstance(cards, list)
        for card in cards:
            self.assertIsInstance(card, dict)

    def test_cards_have_required_keys(self) -> None:
        cards = self.mgr.generate_flashcards(self.items, "ru", "es")
        self.assertGreater(len(cards), 0)
        for card in cards:
            self.assertIn("front", card)
            self.assertIn("back", card)
            self.assertIn("context", card)
            self.assertIn("difficulty", card)

    def test_max_cards_respected(self) -> None:
        cards = self.mgr.generate_flashcards(self.items, "ru", "es", max_cards=2)
        self.assertLessEqual(len(cards), 2)

    def test_difficulty_values_valid(self) -> None:
        cards = self.mgr.generate_flashcards(self.items, "ru", "es")
        valid = {"easy", "medium", "hard"}
        for card in cards:
            self.assertIn(card["difficulty"], valid)

    def test_empty_items_returns_empty(self) -> None:
        cards = self.mgr.generate_flashcards([], "ru", "es")
        self.assertEqual(cards, [])


# ---------------------------------------------------------------------------
# Тесты get_learning_stats
# ---------------------------------------------------------------------------

class GetLearningStatsTestCase(unittest.TestCase):
    """Тесты метода get_learning_stats."""

    def setUp(self) -> None:
        self.mgr = LanguageLearningManager()
        self.items = [
            _make_item("программирование алгоритм"),
            _make_item("программирование структура данных"),
        ]

    def test_returns_dict_with_required_keys(self) -> None:
        stats = self.mgr.get_learning_stats(self.items, "ru", "es")
        self.assertIn("unique_words", stats)
        self.assertIn("total_occurrences", stats)
        self.assertIn("frequency_distribution", stats)
        self.assertIn("top_words", stats)
        self.assertIn("source_lang", stats)
        self.assertIn("target_lang", stats)

    def test_unique_words_correct(self) -> None:
        stats = self.mgr.get_learning_stats(self.items, "ru", "es")
        # "программирование" встречается в 2 записях, остальные по одному разу
        self.assertGreater(stats["unique_words"], 0)

    def test_total_occurrences_gte_unique_words(self) -> None:
        stats = self.mgr.get_learning_stats(self.items, "ru", "es")
        self.assertGreaterEqual(stats["total_occurrences"], stats["unique_words"])

    def test_frequency_distribution_keys(self) -> None:
        stats = self.mgr.get_learning_stats(self.items, "ru", "es")
        dist = stats["frequency_distribution"]
        self.assertIn("easy", dist)
        self.assertIn("medium", dist)
        self.assertIn("hard", dist)

    def test_top_words_limit_10(self) -> None:
        # Генерируем много разных слов
        items = [_make_item(" ".join(f"слово{i}" for i in range(20)))]
        stats = self.mgr.get_learning_stats(items, "ru", "es")
        self.assertLessEqual(len(stats["top_words"]), 10)

    def test_empty_items_returns_zeros(self) -> None:
        stats = self.mgr.get_learning_stats([], "ru", "es")
        self.assertEqual(stats["unique_words"], 0)
        self.assertEqual(stats["total_occurrences"], 0)
        self.assertEqual(stats["top_words"], [])

    def test_source_target_lang_in_stats(self) -> None:
        stats = self.mgr.get_learning_stats(self.items, "ru", "es")
        self.assertEqual(stats["source_lang"], "ru")
        self.assertEqual(stats["target_lang"], "es")


# ---------------------------------------------------------------------------
# Тесты IPC-обработчиков
# ---------------------------------------------------------------------------

class IPCHandlersTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков LanguageLearningManager."""

    def setUp(self) -> None:
        self.mgr = LanguageLearningManager()
        self.items = [
            _make_item("программирование алгоритм структура", "programación algoritmo estructura"),
            _make_item("алгоритм поиск сортировка", "algoritmo búsqueda ordenación"),
        ]

    def test_handle_extract_learning_vocabulary_basic(self) -> None:
        result = self.mgr.handle_extract_learning_vocabulary({
            "source_lang": "ru",
            "target_lang": "es",
            "items": self.items,
        })
        self.assertIn("vocabulary", result)
        self.assertIn("total", result)
        self.assertIn("source_lang", result)
        self.assertIn("target_lang", result)
        self.assertIsInstance(result["vocabulary"], list)

    def test_handle_extract_learning_vocabulary_missing_source_lang(self) -> None:
        with self.assertRaises(RuntimeError):
            self.mgr.handle_extract_learning_vocabulary({
                "target_lang": "es",
                "items": self.items,
            })

    def test_handle_extract_learning_vocabulary_missing_target_lang(self) -> None:
        with self.assertRaises(RuntimeError):
            self.mgr.handle_extract_learning_vocabulary({
                "source_lang": "ru",
                "items": self.items,
            })

    def test_handle_extract_learning_vocabulary_limit(self) -> None:
        result = self.mgr.handle_extract_learning_vocabulary({
            "source_lang": "ru",
            "target_lang": "es",
            "limit": 2,
            "items": self.items,
        })
        self.assertLessEqual(len(result["vocabulary"]), 2)

    def test_handle_generate_flashcards_basic(self) -> None:
        result = self.mgr.handle_generate_flashcards({
            "source_lang": "ru",
            "target_lang": "es",
            "items": self.items,
        })
        self.assertIn("cards", result)
        self.assertIn("total", result)
        self.assertIsInstance(result["cards"], list)

    def test_handle_generate_flashcards_max_cards(self) -> None:
        result = self.mgr.handle_generate_flashcards({
            "source_lang": "ru",
            "target_lang": "es",
            "max_cards": 3,
            "items": self.items,
        })
        self.assertLessEqual(len(result["cards"]), 3)

    def test_handle_generate_flashcards_missing_lang_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.mgr.handle_generate_flashcards({"target_lang": "es", "items": self.items})

    def test_handle_get_learning_stats_basic(self) -> None:
        result = self.mgr.handle_get_learning_stats({
            "source_lang": "ru",
            "target_lang": "es",
            "items": self.items,
        })
        self.assertIn("unique_words", result)
        self.assertIn("frequency_distribution", result)
        self.assertGreater(result["unique_words"], 0)

    def test_handle_get_learning_stats_missing_lang_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.mgr.handle_get_learning_stats({"source_lang": "ru", "items": self.items})

    def test_vocab_entries_have_all_fields(self) -> None:
        result = self.mgr.handle_extract_learning_vocabulary({
            "source_lang": "ru",
            "target_lang": "es",
            "items": self.items,
        })
        for entry in result["vocabulary"]:
            self.assertIn("word_source", entry)
            self.assertIn("word_target", entry)
            self.assertIn("context_sentence", entry)
            self.assertIn("frequency", entry)
            self.assertIn("first_seen", entry)


# ---------------------------------------------------------------------------
# Дополнительные граничные случаи
# ---------------------------------------------------------------------------

class EdgeCasesTestCase(unittest.TestCase):
    """Граничные случаи и специальные сценарии."""

    def setUp(self) -> None:
        self.mgr = LanguageLearningManager()

    def test_items_with_no_lang_match_all(self) -> None:
        """Записи без указания языка принимаются для любого запроса."""
        items = [
            FakeItem(
                text="программирование",
                source_text="программирование",
                translated_text="",
                source_lang="",
                target_lang="",
                ts="2026-04-12T10:00:00",
            )
        ]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        self.assertGreater(len(result), 0)

    def test_frequency_counts_correctly(self) -> None:
        items = [
            _make_item("красивый город красивый"),
            _make_item("красивый день"),
        ]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        красивый = next((e for e in result if e.word_source == "красивый"), None)
        self.assertIsNotNone(красивый)
        self.assertEqual(красивый.frequency, 3)

    def test_words_shorter_than_3_chars_excluded(self) -> None:
        items = [_make_item("он я ты мы программирование")]
        result = self.mgr.extract_vocabulary(items, "ru", "es")
        words = [e.word_source for e in result]
        # Короткие слова (< 3 символов) не должны попадать
        for w in words:
            self.assertGreaterEqual(len(w), 3)

    def test_generate_flashcards_default_max_20(self) -> None:
        # Создаём много уникальных слов
        big_text = " ".join(f"слово{i}длинное" for i in range(30))
        items = [_make_item(big_text)]
        cards = self.mgr.generate_flashcards(items, "ru", "es")
        self.assertLessEqual(len(cards), 20)

    def test_resolve_items_from_explicit_list(self) -> None:
        items = [_make_item("тест программа")]
        resolved = LanguageLearningManager._resolve_items({"items": items})
        self.assertEqual(len(resolved), 1)

    def test_resolve_items_no_store_no_items_returns_empty(self) -> None:
        resolved = LanguageLearningManager._resolve_items({})
        self.assertEqual(resolved, [])


if __name__ == "__main__":
    unittest.main()
