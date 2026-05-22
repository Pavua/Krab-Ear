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


class TestGetStopWordsExtra(unittest.TestCase):
    """Дополнительные тесты get_stop_words."""

    def test_lang_code_uppercase_accepted(self):
        words = StopWords.get_stop_words("RU")
        self.assertGreater(len(words), 100)

    def test_lang_code_with_whitespace(self):
        words = StopWords.get_stop_words("  en  ")
        self.assertGreater(len(words), 100)

    def test_result_is_frozenset_always(self):
        for lang in ("ru", "es", "en", "uk", "zz"):
            result = StopWords.get_stop_words(lang)
            self.assertIsInstance(result, frozenset)

    def test_all_languages_have_reasonable_size(self):
        # Все языки должны иметь > 50 стоп-слов
        for lang in ("ru", "es", "en", "uk"):
            words = StopWords.get_stop_words(lang)
            self.assertGreater(len(words), 50, msg=f"Too few stop words for {lang}")

    def test_frozensets_are_immutable(self):
        words = StopWords.get_stop_words("en")
        with self.assertRaises((AttributeError, TypeError)):
            words.add("newword")  # type: ignore[attr-defined]


class TestIsStopWordExtra(unittest.TestCase):
    """Дополнительные тесты is_stop_word."""

    def test_empty_string_not_stop_word(self):
        self.assertFalse(StopWords.is_stop_word(""))

    def test_whitespace_only_not_stop_word(self):
        self.assertFalse(StopWords.is_stop_word("   "))

    def test_word_with_leading_trailing_spaces(self):
        # strip() применяется внутри is_stop_word
        self.assertTrue(StopWords.is_stop_word("  в  "))

    def test_unknown_lang_returns_false(self):
        self.assertFalse(StopWords.is_stop_word("в", "zz"))

    def test_lang_uppercase_accepted(self):
        self.assertTrue(StopWords.is_stop_word("the", "EN"))

    def test_mixed_case_word(self):
        self.assertTrue(StopWords.is_stop_word("THE", "en"))
        self.assertTrue(StopWords.is_stop_word("И", "ru"))


class TestFilterTextExtra(unittest.TestCase):
    """Дополнительные тесты filter_text."""

    def test_preserves_order(self):
        tokens = ["быстрый", "лиса", "прыгает", "над", "ленивым", "псом"]
        result = StopWords.filter_text(tokens, "ru")
        # Порядок сохраняется
        expected = [t for t in tokens if t not in StopWords.get_stop_words("ru")]
        self.assertEqual(result, expected)

    def test_min_length_default_is_2(self):
        # Слово длиной 1 удаляется по умолчанию
        tokens = ["a", "ok", "word"]
        result = StopWords.filter_text(tokens, "en")
        self.assertNotIn("a", result)

    def test_min_length_1_keeps_short_non_stop_words(self):
        tokens = ["a", "x", "ok"]
        result = StopWords.filter_text(tokens, "en", min_length=1)
        # "a" — стоп-слово en, поэтому удаляется; "x" — нет, остаётся
        self.assertIn("x", result)
        self.assertNotIn("a", result)

    def test_min_length_0_keeps_everything_except_stop_words(self):
        tokens = ["в", "кот"]
        result = StopWords.filter_text(tokens, "ru", min_length=0)
        self.assertNotIn("в", result)
        self.assertIn("кот", result)

    def test_duplicate_words_preserved(self):
        tokens = ["слово", "слово", "кот"]
        result = StopWords.filter_text(tokens, "ru")
        self.assertEqual(result.count("слово"), 2)

    def test_no_lang_and_min_length_combo(self):
        tokens = ["в", "the", "el", "кот", "a"]
        result = StopWords.filter_text(tokens, min_length=3)
        self.assertNotIn("в", result)
        self.assertNotIn("the", result)
        # "кот" длиной 3 и не стоп-слово → должно остаться
        self.assertIn("кот", result)

    def test_returns_list(self):
        result = StopWords.filter_text(["test", "и"])
        self.assertIsInstance(result, list)


class TestSupportedLanguagesExtra(unittest.TestCase):
    def test_returns_list(self):
        langs = StopWords.supported_languages()
        self.assertIsInstance(langs, list)

    def test_all_required_langs_present(self):
        langs = StopWords.supported_languages()
        for lang in ("ru", "es", "en", "uk"):
            self.assertIn(lang, langs)

    def test_no_duplicates(self):
        langs = StopWords.supported_languages()
        self.assertEqual(len(langs), len(set(langs)))

    def test_all_codes_are_strings(self):
        for lang in StopWords.supported_languages():
            self.assertIsInstance(lang, str)


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


class TestWave151SpecNamed(unittest.TestCase):
    """Именованные тесты из спецификации Wave 151."""

    def test_ru_stop_words_loaded(self):
        """RU frozenset загружен и не пуст."""
        words = StopWords.get_stop_words("ru")
        self.assertIsInstance(words, frozenset)
        self.assertGreater(len(words), 50)
        self.assertIn("в", words)

    def test_es_stop_words_loaded(self):
        """ES frozenset загружен и не пуст."""
        words = StopWords.get_stop_words("es")
        self.assertIsInstance(words, frozenset)
        self.assertGreater(len(words), 50)
        self.assertIn("el", words)

    def test_en_stop_words_loaded(self):
        """EN frozenset загружен и не пуст."""
        words = StopWords.get_stop_words("en")
        self.assertIsInstance(words, frozenset)
        self.assertGreater(len(words), 50)
        self.assertIn("the", words)

    def test_lookup_case_insensitive(self):
        """is_stop_word нечувствителен к регистру для всех языков."""
        pairs = [
            ("В", "ru"), ("НА", "ru"),
            ("THE", "en"), ("Is", "en"),
            ("EL", "es"), ("Los", "es"),
        ]
        for word, lang in pairs:
            with self.subTest(word=word, lang=lang):
                self.assertTrue(
                    StopWords.is_stop_word(word, lang),
                    f"Expected {word!r} to be a stop word for {lang!r}",
                )

    def test_no_duplicates_per_language(self):
        """Каждый языковой frozenset не содержит дубликатов (свойство frozenset)."""
        for lang in StopWords.supported_languages():
            words = StopWords.get_stop_words(lang)
            # frozenset by definition has no duplicates; verify by round-tripping through list
            words_list = list(words)
            self.assertEqual(len(words_list), len(set(words_list)),
                             f"Duplicates found in {lang} stop words")

    def test_unicode_well_formed(self):
        """Все стоп-слова во всех языках являются корректными Unicode-строками."""
        for lang in StopWords.supported_languages():
            words = StopWords.get_stop_words(lang)
            for word in words:
                with self.subTest(lang=lang, word=word):
                    self.assertIsInstance(word, str)
                    # Проверка: encode/decode без ошибок
                    encoded = word.encode("utf-8")
                    decoded = encoded.decode("utf-8")
                    self.assertEqual(word, decoded,
                                     f"Word {word!r} in {lang} is not valid UTF-8")
                    # Слово не должно содержать суррогатных символов
                    try:
                        word.encode("utf-16", errors="surrogatepass").decode(
                            "utf-16", errors="surrogatepass"
                        )
                    except UnicodeDecodeError:
                        self.fail(f"Word {word!r} in {lang} has surrogate characters")


if __name__ == "__main__":
    unittest.main()
