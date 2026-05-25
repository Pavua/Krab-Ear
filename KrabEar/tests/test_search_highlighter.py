"""Тесты SearchHighlighter и IPC-метода search_with_highlights."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.history_service import HistoryService
from core.search_highlighter import SearchHighlighter

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SearchHighlighterHighlightTests(unittest.TestCase):
    """Тесты метода SearchHighlighter.highlight()."""

    def setUp(self) -> None:
        self.h = SearchHighlighter()

    # ------------------------------------------------------------------
    # 1. Базовая подсветка одного слова
    # ------------------------------------------------------------------
    def test_single_word_highlight(self) -> None:
        result = self.h.highlight("слово найдено здесь", "найдено")
        self.assertEqual(result, "слово **найдено** здесь")

    # ------------------------------------------------------------------
    # 2. Регистронезависимость — запрос в нижнем регистре, текст в верхнем
    # ------------------------------------------------------------------
    def test_case_insensitive_match(self) -> None:
        result = self.h.highlight("Hello World", "hello")
        self.assertIn("**Hello**", result)

    # ------------------------------------------------------------------
    # 3. Многословный запрос — каждое слово подсвечивается независимо
    # ------------------------------------------------------------------
    def test_multi_word_query(self) -> None:
        result = self.h.highlight("quick brown fox", "quick fox")
        self.assertIn("**quick**", result)
        self.assertIn("**fox**", result)
        self.assertIn("brown", result)  # не совпадает — не должно оборачиваться

    # ------------------------------------------------------------------
    # 4. Пустой запрос — текст возвращается без изменений
    # ------------------------------------------------------------------
    def test_empty_query_returns_original(self) -> None:
        text = "some text here"
        result = self.h.highlight(text, "")
        self.assertEqual(result, text)

    # ------------------------------------------------------------------
    # 5. Пустой текст — возвращается пустая строка
    # ------------------------------------------------------------------
    def test_empty_text_returns_empty(self) -> None:
        result = self.h.highlight("", "query")
        self.assertEqual(result, "")

    # ------------------------------------------------------------------
    # 6. Кастомный маркер
    # ------------------------------------------------------------------
    def test_custom_marker(self) -> None:
        result = self.h.highlight("test text", "test", marker="~~")
        self.assertIn("~~test~~", result)

    # ------------------------------------------------------------------
    # 7. Слово встречается несколько раз в тексте
    # ------------------------------------------------------------------
    def test_multiple_occurrences(self) -> None:
        result = self.h.highlight("cat and cat again", "cat")
        self.assertEqual(result.count("**cat**"), 2)

    # ------------------------------------------------------------------
    # 8. Запрос не найден в тексте — текст без изменений
    # ------------------------------------------------------------------
    def test_no_match_returns_original(self) -> None:
        text = "some unrelated text"
        result = self.h.highlight(text, "notfound")
        self.assertEqual(result, text)


class SearchHighlighterHTMLTests(unittest.TestCase):
    """Тесты метода SearchHighlighter.highlight_html()."""

    def setUp(self) -> None:
        self.h = SearchHighlighter()

    # ------------------------------------------------------------------
    # 9. Базовая HTML-подсветка
    # ------------------------------------------------------------------
    def test_html_highlight_basic(self) -> None:
        result = self.h.highlight_html("found here", "found")
        self.assertIn('<span class="highlight">found</span>', result)

    # ------------------------------------------------------------------
    # 10. Кастомный CSS-класс
    # ------------------------------------------------------------------
    def test_html_custom_css_class(self) -> None:
        result = self.h.highlight_html("test word", "word", css_class="match")
        self.assertIn('<span class="match">word</span>', result)

    # ------------------------------------------------------------------
    # 11. HTML-экранирование спецсимволов в тексте
    # ------------------------------------------------------------------
    def test_html_escaping(self) -> None:
        result = self.h.highlight_html("<b>bold</b> text", "bold")
        # Угловые скобки должны быть экранированы
        self.assertIn("&lt;b&gt;", result)
        self.assertNotIn("<b>bold</b>", result)

    # ------------------------------------------------------------------
    # 12. Пустой запрос — текст экранируется, но без тегов подсветки
    # ------------------------------------------------------------------
    def test_html_empty_query(self) -> None:
        result = self.h.highlight_html("hello & world", "")
        self.assertIn("&amp;", result)
        self.assertNotIn("<span", result)

    # ------------------------------------------------------------------
    # 13. Регистронезависимость в HTML
    # ------------------------------------------------------------------
    def test_html_case_insensitive(self) -> None:
        result = self.h.highlight_html("Hello World", "hello")
        self.assertIn('<span class="highlight">Hello</span>', result)


class SearchHighlighterSnippetsTests(unittest.TestCase):
    """Тесты метода SearchHighlighter.extract_snippets()."""

    def setUp(self) -> None:
        self.h = SearchHighlighter()

    # ------------------------------------------------------------------
    # 14. Базовое извлечение сниппета
    # ------------------------------------------------------------------
    def test_snippet_contains_match(self) -> None:
        text = "много текста до " + "целевое слово" + " и много текста после"
        snippets = self.h.extract_snippets(text, "целевое слово")
        self.assertTrue(len(snippets) >= 1)
        self.assertIn("целевое слово", snippets[0])

    # ------------------------------------------------------------------
    # 15. Многоточие добавляется когда контекст обрезан
    # ------------------------------------------------------------------
    def test_snippet_ellipsis_for_middle_match(self) -> None:
        text = "A" * 100 + " target " + "B" * 100
        snippets = self.h.extract_snippets(text, "target", context_chars=10)
        self.assertTrue(len(snippets) >= 1)
        self.assertTrue(snippets[0].startswith("..."))
        self.assertTrue(snippets[0].endswith("..."))

    # ------------------------------------------------------------------
    # 16. Ограничение max_snippets
    # ------------------------------------------------------------------
    def test_snippet_max_count_respected(self) -> None:
        text = "word here. word there. word again. word once more. word fifth time."
        snippets = self.h.extract_snippets(text, "word", context_chars=5, max_snippets=2)
        self.assertLessEqual(len(snippets), 2)

    # ------------------------------------------------------------------
    # 17. Пустой текст — пустой список
    # ------------------------------------------------------------------
    def test_snippet_empty_text(self) -> None:
        snippets = self.h.extract_snippets("", "query")
        self.assertEqual(snippets, [])

    # ------------------------------------------------------------------
    # 18. Запрос не найден — пустой список
    # ------------------------------------------------------------------
    def test_snippet_no_match(self) -> None:
        snippets = self.h.extract_snippets("some text here", "notfound")
        self.assertEqual(snippets, [])

    # ------------------------------------------------------------------
    # 19. Совпадение в начале текста — нет ведущего многоточия
    # ------------------------------------------------------------------
    def test_snippet_no_leading_ellipsis_at_start(self) -> None:
        text = "target at the very start of this long text body content"
        snippets = self.h.extract_snippets(text, "target", context_chars=5)
        self.assertTrue(len(snippets) >= 1)
        self.assertFalse(snippets[0].startswith("..."))

    # ------------------------------------------------------------------
    # 20. Совпадение в конце текста — нет завершающего многоточия
    # ------------------------------------------------------------------
    def test_snippet_no_trailing_ellipsis_at_end(self) -> None:
        text = "lots of text here at the start and then target"
        snippets = self.h.extract_snippets(text, "target", context_chars=5)
        self.assertTrue(len(snippets) >= 1)
        self.assertFalse(snippets[0].endswith("..."))


class HistoryServiceSearchWithHighlightsTests(unittest.TestCase):
    """Интеграционные тесты handle_search_with_highlights через HistoryService."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

        # Добавляем записи
        self.svc.handle_add_history_item({"text": "Hello world recording here", "paste_status": "ok"})
        self.svc.handle_add_history_item({"text": "Привет мир транскрипция", "paste_status": "ok"})
        self.svc.handle_add_history_item({"text": "completely different content", "paste_status": "ok"})

    # ------------------------------------------------------------------
    # 21. Результат содержит highlighted_text
    # ------------------------------------------------------------------
    def test_result_has_highlighted_text(self) -> None:
        result = self.svc.handle_search_with_highlights({"query": "world"})
        self.assertIn("items", result)
        for item in result["items"]:
            self.assertIn("highlighted_text", item)

    # ------------------------------------------------------------------
    # 22. highlighted_text содержит маркеры вокруг совпадения
    # ------------------------------------------------------------------
    def test_highlighted_text_contains_markers(self) -> None:
        result = self.svc.handle_search_with_highlights({"query": "world"})
        items = result["items"]
        self.assertTrue(len(items) >= 1)
        highlighted = items[0]["highlighted_text"]
        self.assertIn("**world**", highlighted.lower().replace("**world**", "**world**"))

    # ------------------------------------------------------------------
    # 23. Результат содержит snippets
    # ------------------------------------------------------------------
    def test_result_has_snippets(self) -> None:
        result = self.svc.handle_search_with_highlights({"query": "world"})
        for item in result["items"]:
            self.assertIn("snippets", item)
            self.assertIsInstance(item["snippets"], list)

    # ------------------------------------------------------------------
    # 24. Пустой запрос возвращает пустой список
    # ------------------------------------------------------------------
    def test_empty_query_returns_empty(self) -> None:
        result = self.svc.handle_search_with_highlights({"query": ""})
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["next_cursor"])

    # ------------------------------------------------------------------
    # 25. Кастомный маркер передаётся через параметр
    # ------------------------------------------------------------------
    def test_custom_marker_param(self) -> None:
        result = self.svc.handle_search_with_highlights({"query": "world", "marker": "~~"})
        items = result["items"]
        if items:
            highlighted = items[0]["highlighted_text"]
            # Маркер ~~ должен появляться в подсвеченном тексте
            self.assertIn("~~", highlighted)

    # ------------------------------------------------------------------
    # 26. Оригинальные поля item сохраняются в результате
    # ------------------------------------------------------------------
    def test_original_fields_preserved(self) -> None:
        result = self.svc.handle_search_with_highlights({"query": "world"})
        for item in result["items"]:
            self.assertIn("id", item)
            self.assertIn("text", item)
            self.assertIn("ts", item)


class SearchHighlighterHelperTests(unittest.TestCase):
    """Тесты вспомогательных методов."""

    def setUp(self) -> None:
        self.h = SearchHighlighter()

    def test_split_query_single_word(self) -> None:
        result = self.h._split_query("hello")
        self.assertEqual(result, ["hello"])

    def test_split_query_multi_word(self) -> None:
        result = self.h._split_query("hello world")
        self.assertEqual(result, ["hello", "world"])

    def test_split_query_empty(self) -> None:
        result = self.h._split_query("")
        self.assertEqual(result, [])

    def test_split_query_extra_spaces(self) -> None:
        result = self.h._split_query("  foo   bar  ")
        self.assertEqual(result, ["foo", "bar"])

    def test_build_pattern_returns_none_for_empty(self) -> None:
        pattern = self.h._build_pattern("")
        self.assertIsNone(pattern)

    def test_build_pattern_returns_pattern_for_valid(self) -> None:
        import re
        pattern = self.h._build_pattern("hello")
        self.assertIsNotNone(pattern)
        self.assertIsInstance(pattern, re.Pattern)

    def test_overlaps_true(self) -> None:
        self.assertTrue(self.h._overlaps(5, 15, [(10, 20)]))

    def test_overlaps_false_no_overlap(self) -> None:
        self.assertFalse(self.h._overlaps(0, 5, [(10, 20)]))

    def test_overlaps_empty_ranges(self) -> None:
        self.assertFalse(self.h._overlaps(0, 10, []))

    def test_highlight_whitespace_query(self) -> None:
        """Запрос только из пробелов → текст без изменений."""
        text = "some text here"
        result = self.h.highlight(text, "   ")
        self.assertEqual(result, text)

    def test_highlight_none_text(self) -> None:
        """Пустой текст (None-like) возвращается как есть."""
        result = self.h.highlight("", "query")
        self.assertEqual(result, "")

    def test_highlight_html_empty_text(self) -> None:
        """highlight_html с пустым текстом возвращает пустую строку."""
        result = self.h.highlight_html("", "query")
        self.assertEqual(result, "")

    def test_highlight_preserves_surrounding_text(self) -> None:
        """Текст вокруг совпадения не изменяется."""
        result = self.h.highlight("before match after", "match")
        self.assertIn("before", result)
        self.assertIn("after", result)

    def test_highlight_html_no_match(self) -> None:
        """highlight_html без совпадения — нет тегов span."""
        result = self.h.highlight_html("hello world", "notfound")
        self.assertNotIn("<span", result)
        self.assertIn("hello world", result)

    def test_snippet_paginates_multiple_matches(self) -> None:
        """Несколько совпадений возвращают несколько сниппетов."""
        text = "A" * 200 + " target " + "B" * 200 + " target " + "C" * 200
        snippets = self.h.extract_snippets(text, "target", context_chars=5, max_snippets=3)
        self.assertGreaterEqual(len(snippets), 2)

    def test_highlight_multi_word_each_highlighted(self) -> None:
        """Каждое слово из многословного запроса подсвечивается отдельно."""
        result = self.h.highlight("the cat sat on the mat", "cat mat")
        self.assertIn("**cat**", result)
        self.assertIn("**mat**", result)
        self.assertNotIn("**the**", result)
        self.assertNotIn("**on**", result)


class SearchHighlighterWave131Tests(unittest.TestCase):
    """Wave 131 required test cases for SearchHighlighter."""

    def setUp(self) -> None:
        self.h = SearchHighlighter()

    # ------------------------------------------------------------------
    # test_single_word_match_highlighted
    # ------------------------------------------------------------------
    def test_single_word_match_highlighted(self) -> None:
        result = self.h.highlight("the quick brown fox", "quick")
        self.assertIn("**quick**", result)
        self.assertNotIn("**the**", result)
        self.assertNotIn("**brown**", result)

    # ------------------------------------------------------------------
    # test_multiple_matches
    # ------------------------------------------------------------------
    def test_multiple_matches(self) -> None:
        result = self.h.highlight("cat on a cat mat", "cat")
        self.assertEqual(result.count("**cat**"), 2)
        self.assertNotIn("**on**", result)
        self.assertNotIn("**mat**", result)

    # ------------------------------------------------------------------
    # test_no_match_unchanged
    # ------------------------------------------------------------------
    def test_no_match_unchanged(self) -> None:
        text = "nothing relevant here"
        result = self.h.highlight(text, "zzzmissing")
        self.assertEqual(result, text)
        self.assertNotIn("**", result)

    # ------------------------------------------------------------------
    # test_case_insensitive_match
    # ------------------------------------------------------------------
    def test_case_insensitive_match(self) -> None:
        result = self.h.highlight("UPPER lower MiXeD", "upper")
        self.assertIn("**UPPER**", result)
        result2 = self.h.highlight("Hello World", "WORLD")
        self.assertIn("**World**", result2)

    # ------------------------------------------------------------------
    # test_unicode_query
    # ------------------------------------------------------------------
    def test_unicode_query(self) -> None:
        result = self.h.highlight("привет мир и снова привет", "мир")
        self.assertIn("**мир**", result)
        # Surrounding cyrillic words untouched
        self.assertIn("привет", result)

    # ------------------------------------------------------------------
    # test_html_escape_safe (no XSS)
    # ------------------------------------------------------------------
    def test_html_escape_safe(self) -> None:
        """highlight_html must not allow script injection (XSS)."""
        malicious_text = '<script>alert("xss")</script> safe word here'
        result = self.h.highlight_html(malicious_text, "safe")
        # Script tag must be escaped
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)
        # Query match should still be highlighted
        self.assertIn('<span class="highlight">safe</span>', result)

    def test_html_escape_ampersand_and_quotes(self) -> None:
        """Ampersand and quotes in text are escaped in HTML output."""
        result = self.h.highlight_html('find "this" & that', "find")
        self.assertIn("&amp;", result)
        self.assertIn("&quot;", result)
        self.assertIn('<span class="highlight">find</span>', result)

    def test_html_escape_no_double_escape_in_highlight(self) -> None:
        """When query itself contains HTML-special chars, they are escaped in result."""
        # '&' in query — html.escape("&") = "&amp;" so the pattern should still match
        result = self.h.highlight_html("a & b are here", "&")
        # The escaped form &amp; should be wrapped in a span
        self.assertIn("<span", result)

    # ------------------------------------------------------------------
    # test_empty_query
    # ------------------------------------------------------------------
    def test_empty_query(self) -> None:
        text = "some text"
        self.assertEqual(self.h.highlight(text, ""), text)
        self.assertEqual(self.h.highlight(text, "   "), text)
        # HTML path: empty query escapes but no spans
        html_result = self.h.highlight_html("a < b", "")
        self.assertIn("&lt;", html_result)
        self.assertNotIn("<span", html_result)

    # ------------------------------------------------------------------
    # test_concurrent_highlight
    # ------------------------------------------------------------------
    def test_concurrent_highlight(self) -> None:
        """Concurrent calls from multiple threads return correct results."""
        import threading
        results = {}
        errors = []

        def worker(idx: int) -> None:
            try:
                text = f"word{idx} middle word{idx}"
                query = f"word{idx}"
                res = self.h.highlight(text, query)
                results[idx] = res
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        for idx, res in results.items():
            marker = f"**word{idx}**"
            self.assertEqual(res.count(marker), 2, f"thread {idx}: expected 2 occurrences in {res!r}")


if __name__ == "__main__":
    unittest.main()
