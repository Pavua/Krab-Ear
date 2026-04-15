"""Тесты для core.stop_words.StopWords."""
from core.stop_words import StopWords
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestGetStopWords(unittest.TestCase):
    def test_ru_nonempty(self):
        words = StopWords.get_stop_words("ru")
        self.assertIsInstance(words, frozenset)
        self.assertGreater(len(words), 100)

    def test_es_nonempty(self):
        words = StopWords.get_stop_words("es")
        self.assertIsInstance(words, frozenset)
        self.assertGreater(len(words), 80)

    def test_en_nonempty(self):
        words = StopWords.get_stop_words("en")
        self.assertIsInstance(words, frozenset)
        self.assertGreater(len(words), 100)

    def test_uk_nonempty(self):
        words = StopWords.get_stop_words("uk")
        self.assertIsInstance(words, frozenset)
        self.assertGreater(len(words), 60)

    def test_unknown_lang_returns_empty(self):
        self.assertEqual(StopWords.get_stop_words("zz"), frozenset())


class TestIsStopWord(unittest.TestCase):
    def test_ru_stop_word(self):
        self.assertTrue(StopWords.is_stop_word("в"))
        self.assertTrue(StopWords.is_stop_word("на"))
        self.assertTrue(StopWords.is_stop_word("был"))

    def test_ru_stop_word_case_insensitive(self):
        self.assertTrue(StopWords.is_stop_word("В"))
        self.assertTrue(StopWords.is_stop_word("НА"))

    def test_en_stop_word(self):
        self.assertTrue(StopWords.is_stop_word("the"))
        self.assertTrue(StopWords.is_stop_word("and"))
        self.assertTrue(StopWords.is_stop_word("is"))

    def test_es_stop_word(self):
        self.assertTrue(StopWords.is_stop_word("el"))
        self.assertTrue(StopWords.is_stop_word("que"))
        self.assertTrue(StopWords.is_stop_word("una"))

    def test_uk_stop_word(self):
        self.assertTrue(StopWords.is_stop_word("і"))
        self.assertTrue(StopWords.is_stop_word("та"))

    def test_non_stop_word(self):
        self.assertFalse(StopWords.is_stop_word("кошка"))
        self.assertFalse(StopWords.is_stop_word("transcription"))
        self.assertFalse(StopWords.is_stop_word("reunión"))

    def test_with_language_filter_ru(self):
        self.assertTrue(StopWords.is_stop_word("в", "ru"))
        # "the" is EN — should NOT match when language="ru"
        self.assertFalse(StopWords.is_stop_word("the", "ru"))

    def test_with_language_filter_en(self):
        self.assertTrue(StopWords.is_stop_word("the", "en"))
        # "в" is RU — should NOT match when language="en"
        self.assertFalse(StopWords.is_stop_word("в", "en"))

    def test_no_language_matches_any(self):
        # without language arg all languages are checked
        self.assertTrue(StopWords.is_stop_word("the"))
        self.assertTrue(StopWords.is_stop_word("в"))
        self.assertTrue(StopWords.is_stop_word("el"))
        self.assertTrue(StopWords.is_stop_word("та"))


class TestFilterText(unittest.TestCase):
    def test_filters_ru_stop_words(self):
        tokens = ["я", "иду", "в", "магазин"]
        result = StopWords.filter_text(tokens, "ru")
        self.assertNotIn("в", result)
        self.assertNotIn("я", result)
        self.assertIn("иду", result)
        self.assertIn("магазин", result)

    def test_filters_en_stop_words(self):
        tokens = ["the", "quick", "brown", "fox", "is", "running"]
        result = StopWords.filter_text(tokens, "en")
        self.assertNotIn("the", result)
        self.assertNotIn("is", result)
        self.assertIn("quick", result)
        self.assertIn("running", result)

    def test_filters_es_stop_words(self):
        tokens = ["el", "gato", "está", "en", "la", "casa"]
        result = StopWords.filter_text(tokens, "es")
        self.assertNotIn("el", result)
        self.assertNotIn("la", result)
        self.assertIn("gato", result)
        self.assertIn("casa", result)

    def test_min_length_filter(self):
        tokens = ["a", "ok", "the", "word"]
        result = StopWords.filter_text(tokens, "en", min_length=3)
        self.assertNotIn("a", result)
        self.assertNotIn("ok", result)
        self.assertIn("word", result)

    def test_no_language_filters_all(self):
        tokens = ["в", "the", "el", "кот"]
        result = StopWords.filter_text(tokens)
        self.assertNotIn("в", result)
        self.assertNotIn("the", result)
        self.assertNotIn("el", result)
        self.assertIn("кот", result)

    def test_empty_input(self):
        self.assertEqual(StopWords.filter_text([]), [])

    def test_all_stop_words(self):
        tokens = ["в", "и", "на"]
        result = StopWords.filter_text(tokens, "ru")
        self.assertEqual(result, [])


class TestSupportedLanguages(unittest.TestCase):
    def test_contains_required_langs(self):
        langs = StopWords.supported_languages()
        for lang in ("ru", "es", "en", "uk"):
            self.assertIn(lang, langs)


class TestHistoryServiceUsesStopWords(unittest.TestCase):
    """Smoke-тест: _STOP_WORDS в HistoryService содержит слова из всех 4 языков."""

    def test_stop_words_imported_in_history_service(self):
        # Проверяем что импорт происходит без ошибок
        from backend.history_service import HistoryService
        sw = HistoryService._STOP_WORDS
        self.assertIsInstance(sw, frozenset)
        # Русское
        self.assertIn("в", sw)
        # Английское
        self.assertIn("the", sw)
        # Испанское
        self.assertIn("el", sw)
        # Украинское
        self.assertIn("та", sw)
        # Должно быть существенно больше, чем было раньше (~100 слов)
        self.assertGreater(len(sw), 150)


if __name__ == "__main__":
    unittest.main()
