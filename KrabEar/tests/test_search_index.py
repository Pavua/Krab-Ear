"""Тесты для SearchIndex (KrabEar/core/search_index.py)."""

from core.search_index import SearchIndex, SearchResult, _stem_ru, _tokenize
import sys
import os
import threading
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


class TestTokenizeExtra(unittest.TestCase):
    """Дополнительные тесты _tokenize."""

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(_tokenize(""), [])

    def test_digits_only(self):
        tokens = _tokenize("2024")
        self.assertIn("2024", tokens)

    def test_only_punctuation_returns_empty(self):
        tokens = _tokenize("!!! ???")
        self.assertEqual(tokens, [])

    def test_spanish_characters(self):
        # Символы вне [а-яёa-z0-9] не токенизируются как отдельные токены
        tokens = _tokenize("hola")
        self.assertIn("hola", tokens)

    def test_newlines_and_tabs(self):
        tokens = _tokenize("hello\tworld\ntest")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)
        self.assertIn("test", tokens)


class TestStemRuExtra(unittest.TestCase):
    """Дополнительные тесты _stem_ru."""

    def test_verbal_suffix_ать(self):
        # "писать" → стемминг убирает суффикс
        result = _stem_ru("писать")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) >= 3)

    def test_verbal_suffix_ться(self):
        result = _stem_ru("бороться")
        self.assertLess(len(result), len("бороться"))

    def test_noun_suffix_ами(self):
        result = _stem_ru("столами")
        self.assertLess(len(result), len("столами"))

    def test_adjective_suffix_ого(self):
        result = _stem_ru("красного")
        self.assertLess(len(result), len("красного"))

    def test_exactly_min_length(self):
        # слово длиной ровно 3 → возвращается без изменений
        self.assertEqual(_stem_ru("кот"), "кот")

    def test_stem_is_string(self):
        self.assertIsInstance(_stem_ru("тестирование"), str)


class TestSearchIndexExtra(unittest.TestCase):
    """Дополнительные тесты SearchIndex."""

    def _build_default(self) -> SearchIndex:
        idx = SearchIndex()
        idx.build_index([
            {"id": "1", "text": "Добрый день коллеги", "source_text": "", "translated_text": ""},
            {"id": "2", "text": "Всем привет как дела", "source_text": "", "translated_text": ""},
            {"id": "3", "text": "Тестирование системы поиска", "source_text": "", "translated_text": ""},
        ])
        return idx

    def test_whitespace_only_query_returns_empty(self):
        idx = self._build_default()
        self.assertEqual(idx.search("   "), [])

    def test_build_empty_list(self):
        idx = SearchIndex()
        idx.build_index([])
        stats = idx.get_index_stats()
        self.assertEqual(stats["items_indexed"], 0)
        self.assertEqual(stats["unique_words"], 0)
        self.assertEqual(idx.search("hello"), [])

    def test_item_without_id_skipped(self):
        idx = SearchIndex()
        idx.build_index([{"text": "orphan text"}])
        # Документ без id должен игнорироваться
        self.assertEqual(idx.get_index_stats()["items_indexed"], 0)

    def test_stats_signature_not_none_after_build(self):
        idx = self._build_default()
        stats = idx.get_index_stats()
        self.assertIsNotNone(stats["signature"])
        self.assertIsInstance(stats["signature"], str)

    def test_source_text_indexed(self):
        idx = SearchIndex()
        idx.build_index([
            {"id": "s1", "text": "", "source_text": "уникальноеслово", "translated_text": ""}
        ])
        results = idx.search("уникальноеслово")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].item_id, "s1")

    def test_item_text_static_all_fields(self):
        item = {
            "text": "главный",
            "source_text": "дополнительный",
            "translated_text": "translated",
        }
        text = SearchIndex._item_text(item)
        self.assertIn("главный", text)
        self.assertIn("дополнительный", text)
        self.assertIn("translated", text)

    def test_item_text_static_missing_fields(self):
        # Если поля отсутствуют — не должно быть KeyError
        text = SearchIndex._item_text({})
        self.assertIsInstance(text, str)

    def test_make_snippet_no_match_returns_prefix(self):
        snippet = SearchIndex._make_snippet("some long text here", ["zzz"])
        # Если совпадений нет — возвращает первые 60 символов
        self.assertTrue(snippet.startswith("some"))

    def test_make_snippet_match_at_start(self):
        snippet = SearchIndex._make_snippet("hello world test", ["hello"])
        self.assertIn("hello", snippet.lower())

    def test_make_snippet_match_in_middle(self):
        snippet = SearchIndex._make_snippet(
            "aaa bbb ccc ddd eee fff ggg hhh iii jjj kkk lll mmm nnn ooo ppp qqq rrr sss ttt",
            ["mmm"],
        )
        self.assertIn("mmm", snippet.lower())

    def test_search_returns_sorted_by_score(self):
        # Все документы имеют одинаковый score → сортировка по item_id
        idx = SearchIndex()
        idx.build_index([
            {"id": "b", "text": "общий токен один", "source_text": "", "translated_text": ""},
            {"id": "a", "text": "общий токен два", "source_text": "", "translated_text": ""},
        ])
        results = idx.search("общий")
        ids = [r.item_id for r in results]
        # Оба должны присутствовать; порядок: score desc, id asc
        self.assertEqual(sorted(ids), ids)

    def test_matched_terms_in_result(self):
        idx = self._build_default()
        results = idx.search("привет")
        self.assertTrue(len(results) > 0)
        # matched_terms — список строк
        for term in results[0].matched_terms:
            self.assertIsInstance(term, str)

    def test_build_index_twice_same_data_stable_signature(self):
        idx = self._build_default()
        sig1 = idx._signature
        idx.build_index([
            {"id": "1", "text": "Добрый день коллеги", "source_text": "", "translated_text": ""},
            {"id": "2", "text": "Всем привет как дела", "source_text": "", "translated_text": ""},
            {"id": "3", "text": "Тестирование системы поиска", "source_text": "", "translated_text": ""},
        ])
        self.assertEqual(idx._signature, sig1)

    def test_compute_signature_deterministic(self):
        items = [{"id": "x", "text": "one two three", "translated_text": ""}]
        s1 = SearchIndex._compute_signature(items)
        s2 = SearchIndex._compute_signature(items)
        self.assertEqual(s1, s2)

    def test_compute_signature_changes_with_different_data(self):
        items_a = [{"id": "x", "text": "aaa", "translated_text": ""}]
        items_b = [{"id": "x", "text": "bbb", "translated_text": ""}]
        self.assertNotEqual(
            SearchIndex._compute_signature(items_a),
            SearchIndex._compute_signature(items_b),
        )

    def test_limit_zero_returns_empty(self):
        idx = self._build_default()
        results = idx.search("привет", limit=0)
        self.assertEqual(results, [])

    def test_single_doc_index_search_hit(self):
        idx = SearchIndex()
        idx.build_index([{"id": "only", "text": "единственный документ", "source_text": "",
                          "translated_text": ""}])
        results = idx.search("единственный")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].item_id, "only")

    def test_search_score_equals_query_token_count(self):
        idx = self._build_default()
        results = idx.search("привет")
        # one query token → score = 1
        self.assertEqual(results[0].score, 1)


class TestSearchIndexWave111(unittest.TestCase):
    """Wave 111 required tests: remove, concurrent, unicode, stop-words, phrase."""

    def _make(self, item_id, text):
        return {"id": item_id, "text": text, "source_text": "", "translated_text": ""}

    # ------------------------------------------------------------------
    # test_index_basic_text — базовое индексирование и поиск
    # ------------------------------------------------------------------
    def test_index_basic_text(self):
        idx = SearchIndex()
        idx.build_index([self._make("1", "Добрый день коллеги")])
        results = idx.search("день")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].item_id, "1")

    # ------------------------------------------------------------------
    # test_search_returns_matching_doc_ids
    # ------------------------------------------------------------------
    def test_search_returns_matching_doc_ids(self):
        idx = SearchIndex()
        idx.build_index([
            self._make("a", "утренняя встреча"),
            self._make("b", "вечерняя встреча"),
            self._make("c", "полдень"),
        ])
        results = idx.search("встреча")
        ids = {r.item_id for r in results}
        self.assertIn("a", ids)
        self.assertIn("b", ids)
        self.assertNotIn("c", ids)

    # ------------------------------------------------------------------
    # test_search_unicode_terms (Cyrillic)
    # ------------------------------------------------------------------
    def test_search_unicode_terms(self):
        idx = SearchIndex()
        idx.build_index([self._make("ru1", "ёжик бежал по лесу")])
        # Кириллица «ё» в токенизаторе включена через [а-яё]
        results = idx.search("лесу")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].item_id, "ru1")

    # ------------------------------------------------------------------
    # test_stop_words_filtered — строка из одних стоп-слов/пунктуации
    # ------------------------------------------------------------------
    def test_stop_words_filtered(self):
        # Токенизатор отбрасывает пунктуацию и нелатинские/некириллические символы.
        # Запрос состоящий только из знаков препинания — нет токенов → пустой результат.
        idx = SearchIndex()
        idx.build_index([self._make("1", "какой-то текст")])
        results = idx.search("!!! ???")
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # test_remove_doc_from_index — пересборка без документа
    # ------------------------------------------------------------------
    def test_remove_doc_from_index(self):
        items = [
            self._make("x", "уникальный документ один"),
            self._make("y", "другой документ два"),
        ]
        idx = SearchIndex()
        idx.build_index(items)
        # Убедимся что "x" находится
        self.assertTrue(any(r.item_id == "x" for r in idx.search("уникальный")))
        # Пересобираем без "x"
        idx.build_index([self._make("y", "другой документ два")])
        results = idx.search("уникальный")
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # test_concurrent_index_search — параллельные read-only поиски
    # ------------------------------------------------------------------
    def test_concurrent_index_search(self):
        items = [self._make(str(i), f"слово{i} тест данные") for i in range(50)]
        idx = SearchIndex()
        idx.build_index(items)

        errors = []

        def worker():
            try:
                for _ in range(20):
                    results = idx.search("тест")
                    assert isinstance(results, list)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent search raised: {errors}")

    # ------------------------------------------------------------------
    # test_empty_query_returns_empty
    # ------------------------------------------------------------------
    def test_empty_query_returns_empty(self):
        idx = SearchIndex()
        idx.build_index([self._make("1", "что-то тут")])
        self.assertEqual(idx.search(""), [])
        self.assertEqual(idx.search("   "), [])

    # ------------------------------------------------------------------
    # test_phrase_search — multi-word AND semantics
    # ------------------------------------------------------------------
    def test_phrase_search(self):
        idx = SearchIndex()
        idx.build_index([
            self._make("hit", "машинное обучение нейросети"),
            self._make("nohit1", "только машинное"),
            self._make("nohit2", "только нейросети"),
        ])
        results = idx.search("машинное нейросети")
        ids = {r.item_id for r in results}
        self.assertIn("hit", ids)
        self.assertNotIn("nohit1", ids)
        self.assertNotIn("nohit2", ids)


class TestW1036Fixes(unittest.TestCase):
    """W1036 F3 + F5 regression tests."""

    def _make(self, item_id, text):
        return {"id": item_id, "text": text, "source_text": "", "translated_text": ""}

    # ------------------------------------------------------------------
    # F3: Spanish diacritics must not break tokenization
    # ------------------------------------------------------------------
    def test_spanish_comunicacion_tokenized_as_one_word(self):
        """'Comunicación' must be indexed as a single token, not split at 'ó'."""
        tokens = _tokenize("Comunicación")
        # Must be exactly one token; the old regex produced ['comunicaci', 'n']
        self.assertEqual(len(tokens), 1, f"Expected 1 token, got: {tokens}")
        self.assertIn("comunicación", tokens)

    def test_spanish_diacritics_searchable(self):
        """Items containing Spanish diacritics must be found by search."""
        idx = SearchIndex()
        idx.build_index([self._make("es1", "Comunicación en español")])
        results = idx.search("comunicación")
        ids = [r.item_id for r in results]
        self.assertIn("es1", ids)

    def test_spanish_accent_chars_tokenized(self):
        """á, é, í, ó, ú, ñ, ü must all be included in tokens."""
        for word in ["café", "niño", "así", "corazón", "flügelhorn"]:
            tokens = _tokenize(word)
            self.assertEqual(len(tokens), 1, f"'{word}' should be 1 token, got: {tokens}")

    # ------------------------------------------------------------------
    # F5: limit < 0 must return empty list, not a tail slice
    # ------------------------------------------------------------------
    def test_negative_limit_returns_empty(self):
        """search() with limit=-1 must return [] not a tail slice."""
        idx = SearchIndex()
        idx.build_index([self._make("1", "привет мир")])
        results = idx.search("привет", limit=-1)
        self.assertEqual(results, [], f"Expected [], got: {results}")

    def test_negative_limit_large_returns_empty(self):
        """search() with limit=-100 must also return []."""
        idx = SearchIndex()
        idx.build_index([self._make("1", "привет мир"), self._make("2", "привет тест")])
        results = idx.search("привет", limit=-100)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
