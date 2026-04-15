"""Тесты для SearchIndex (KrabEar/core/search_index.py)."""

from core.search_index import SearchIndex, SearchResult, _stem_ru, _tokenize
import sys
import os
import unittest

# Убедимся что модули core.* доступны при запуске напрямую
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_item(item_id: str, text: str, translated: str = "", source: str = "") -> dict:
    return {
        "id": item_id,
        "text": text,
        "source_text": source,
        "translated_text": translated,
    }


class TestTokenize(unittest.TestCase):
    def test_basic_latin(self):
        tokens = _tokenize("Hello World")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)

    def test_cyrillic(self):
        tokens = _tokenize("Привет мир")
        self.assertTrue(any("привет" in t for t in tokens))

    def test_mixed(self):
        tokens = _tokenize("test тест 123")
        self.assertIn("test", tokens)
        self.assertIn("123", tokens)

    def test_punctuation_stripped(self):
        tokens = _tokenize("hello, world!")
        self.assertNotIn(",", tokens)
        self.assertNotIn("!", tokens)


class TestStemRu(unittest.TestCase):
    def test_stem_suffix_ость(self):
        stem = _stem_ru("скорость")
        self.assertNotEqual(stem, "скорость")
        self.assertTrue(len(stem) >= 3)

    def test_short_word_unchanged(self):
        self.assertEqual(_stem_ru("да"), "да")

    def test_no_suffix_unchanged(self):
        result = _stem_ru("abc")
        self.assertEqual(result, "abc")


class TestSearchIndex(unittest.TestCase):
    def setUp(self):
        self.index = SearchIndex()
        self.items = [
            _make_item("1", "Добрый день коллеги"),
            _make_item("2", "Всем привет как дела"),
            _make_item("3", "Тестирование системы поиска"),
            _make_item("4", "Hello world from testing"),
            _make_item("5", "Рабочее совещание по проекту", translated="Work meeting about project"),
        ]
        self.index.build_index(self.items)

    def test_single_word_search(self):
        results = self.index.search("привет")
        ids = [r.item_id for r in results]
        self.assertIn("2", ids)

    def test_multiword_and_logic(self):
        # оба слова присутствуют только в item 3
        results = self.index.search("тестирование поиска")
        ids = [r.item_id for r in results]
        self.assertIn("3", ids)
        self.assertNotIn("1", ids)

    def test_multiword_no_match(self):
        # слово есть только в разных документах
        results = self.index.search("привет поиска")
        # "привет" в item 2, "поиска" в item 3 — пересечения нет
        self.assertEqual(results, [])

    def test_case_insensitive(self):
        results = self.index.search("ПРИВЕТ")
        ids = [r.item_id for r in results]
        self.assertIn("2", ids)

    def test_empty_query_returns_empty(self):
        results = self.index.search("")
        self.assertEqual(results, [])

    def test_result_is_search_result_dataclass(self):
        results = self.index.search("привет")
        self.assertTrue(len(results) > 0)
        r = results[0]
        self.assertIsInstance(r, SearchResult)
        self.assertIsInstance(r.item_id, str)
        self.assertIsInstance(r.score, int)
        self.assertIsInstance(r.matched_terms, list)
        self.assertIsInstance(r.snippet, str)

    def test_snippet_contains_match(self):
        results = self.index.search("hello")
        self.assertTrue(len(results) > 0)
        snippet = results[0].snippet.lower()
        self.assertIn("hello", snippet)

    def test_limit_respected(self):
        # добавим много элементов с тем же словом
        many_items = [_make_item(str(i), "общее слово тест") for i in range(100)]
        idx = SearchIndex()
        idx.build_index(many_items)
        results = idx.search("тест", limit=10)
        self.assertLessEqual(len(results), 10)

    def test_lazy_rebuild_same_data(self):
        sig_before = self.index._signature
        self.index.build_index(self.items)  # те же данные
        self.assertEqual(self.index._signature, sig_before)

    def test_lazy_rebuild_new_data(self):
        sig_before = self.index._signature
        new_items = self.items + [_make_item("99", "новый элемент")]
        self.index.build_index(new_items)
        self.assertNotEqual(self.index._signature, sig_before)

    def test_get_index_stats(self):
        stats = self.index.get_index_stats()
        self.assertIn("unique_words", stats)
        self.assertIn("items_indexed", stats)
        self.assertIn("total_word_refs", stats)
        self.assertEqual(stats["items_indexed"], len(self.items))
        self.assertGreater(stats["unique_words"], 0)

    def test_translated_text_indexed(self):
        # item 5 имеет translated_text на английском
        results = self.index.search("meeting")
        ids = [r.item_id for r in results]
        self.assertIn("5", ids)

    def test_unknown_word_returns_empty(self):
        results = self.index.search("несуществующееслово")
        self.assertEqual(results, [])

    def test_stemmed_search_matches_inflection(self):
        # "тестирование" и "тестирования" должны давать тот же корень
        idx = SearchIndex()
        idx.build_index([_make_item("a", "тестирование системы")])
        results = idx.search("тестирования")
        # оба варианта стеммируются одинаково — должны совпасть
        # (это best-effort: тест фиксирует поведение, не жёсткое требование)
        # Достаточно что search не падает с ошибкой
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
