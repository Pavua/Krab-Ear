"""Тесты DuplicateDetector — обнаружение дублирующихся транскрипций.

Покрытие:
- is_duplicate: точное совпадение, высокое сходство, низкое сходство
- find_duplicates: пустой список, нет дублей, одна группа, несколько групп
- временное окно: записи вне 60-секундного окна не объединяются
- порог: настраиваемый threshold
- handle_find_duplicates в HistoryService
"""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore
from core.duplicate_detector import DuplicateDetector

import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_item(text: str, ts: float | None = None) -> dict:
    return {"text": text, "ts": ts or time.time()}


class IsDuplicateTestCase(unittest.TestCase):
    """is_duplicate — базовые сценарии."""

    def setUp(self) -> None:
        self.det = DuplicateDetector()

    def test_exact_match_is_duplicate(self) -> None:
        self.assertTrue(self.det.is_duplicate("hello world", "hello world"))

    def test_high_similarity_is_duplicate(self) -> None:
        # Одна буква отличается — всё равно выше 0.9 для длинного текста
        text1 = "The quick brown fox jumps over the lazy dog"
        text2 = "The quick brown fox jumps over the lazy dot"
        self.assertTrue(self.det.is_duplicate(text1, text2, threshold=0.9))

    def test_low_similarity_not_duplicate(self) -> None:
        self.assertFalse(self.det.is_duplicate("hello world", "foo bar baz"))

    def test_empty_strings_not_duplicate(self) -> None:
        self.assertFalse(self.det.is_duplicate("", "hello"))
        self.assertFalse(self.det.is_duplicate("hello", ""))
        self.assertFalse(self.det.is_duplicate("", ""))

    def test_custom_threshold_lower(self) -> None:
        # При пороге 0.5 два похожих предложения считаются дублями
        self.assertTrue(self.det.is_duplicate("hello world", "hello earth", threshold=0.5))

    def test_custom_threshold_higher(self) -> None:
        # При пороге 1.0 только точное совпадение
        self.assertFalse(self.det.is_duplicate("hello world", "hello worlds", threshold=1.0))
        self.assertTrue(self.det.is_duplicate("hello world", "hello world", threshold=1.0))


class FindDuplicatesTestCase(unittest.TestCase):
    """find_duplicates — группировка похожих записей."""

    def setUp(self) -> None:
        self.det = DuplicateDetector()
        self.base_ts = 1_700_000_000.0

    def test_empty_list_returns_empty(self) -> None:
        self.assertEqual(self.det.find_duplicates([]), [])

    def test_single_item_no_duplicates(self) -> None:
        items = [_make_item("unique text", self.base_ts)]
        self.assertEqual(self.det.find_duplicates(items), [])

    def test_no_similar_items(self) -> None:
        items = [
            _make_item("hello world", self.base_ts),
            _make_item("completely different text here", self.base_ts + 5),
            _make_item("another unrelated sentence", self.base_ts + 10),
        ]
        self.assertEqual(self.det.find_duplicates(items), [])

    def test_two_identical_items_one_group(self) -> None:
        items = [
            _make_item("Привет мир", self.base_ts),
            _make_item("Привет мир", self.base_ts + 2),
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].items), 2)
        self.assertAlmostEqual(groups[0].similarity, 1.0, places=2)

    def test_three_similar_items_one_group(self) -> None:
        long_base = "The quick brown fox jumps over the lazy dog today"
        items = [
            _make_item(long_base, self.base_ts),
            _make_item(long_base + "!", self.base_ts + 3),
            _make_item(long_base + ".", self.base_ts + 6),
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 1)
        self.assertGreaterEqual(len(groups[0].items), 2)

    def test_time_window_excludes_distant_items(self) -> None:
        text = "Transcription text example"
        items = [
            _make_item(text, self.base_ts),
            _make_item(text, self.base_ts + 120),  # 2 minutes apart — outside window
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 0)

    def test_time_window_includes_close_items(self) -> None:
        text = "Transcription text example"
        items = [
            _make_item(text, self.base_ts),
            _make_item(text, self.base_ts + 30),  # within 60s window
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 1)

    def test_two_separate_duplicate_groups(self) -> None:
        text_a = "First repeated sentence here"
        text_b = "Second repeated sentence there"
        items = [
            _make_item(text_a, self.base_ts),
            _make_item(text_a, self.base_ts + 5),
            _make_item(text_b, self.base_ts + 10),
            _make_item(text_b, self.base_ts + 15),
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 2)

    def test_group_similarity_value_set(self) -> None:
        items = [
            _make_item("Hello World", self.base_ts),
            _make_item("Hello World", self.base_ts + 1),
        ]
        groups = self.det.find_duplicates(items)
        self.assertGreater(groups[0].similarity, 0.0)
        self.assertLessEqual(groups[0].similarity, 1.0)

    def test_items_without_timestamp_are_compared(self) -> None:
        # Записи без ts — временное окно не проверяется, сравниваются всегда
        items = [
            {"text": "Same text"},
            {"text": "Same text"},
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 1)


class HistoryServiceFindDuplicatesTestCase(unittest.TestCase):
    """handle_find_duplicates через HistoryService."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def _add(self, text: str) -> None:
        self.svc.handle_add_history_item({"text": text, "paste_status": "success"})

    def test_empty_history_no_groups(self) -> None:
        result = self.svc.handle_find_duplicates({})
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["total_duplicates"], 0)

    def test_finds_duplicates_in_history(self) -> None:
        self._add("Hello from Krab Ear")
        self._add("Hello from Krab Ear")
        result = self.svc.handle_find_duplicates({"similarity_threshold": 0.9})
        self.assertGreaterEqual(len(result["groups"]), 1)
        self.assertGreaterEqual(result["total_duplicates"], 1)

    def test_no_duplicates_returns_empty_groups(self) -> None:
        self._add("First unique sentence")
        self._add("Completely different content here")
        result = self.svc.handle_find_duplicates({})
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["total_duplicates"], 0)

    def test_result_structure(self) -> None:
        self._add("Test sentence repeated")
        self._add("Test sentence repeated")
        result = self.svc.handle_find_duplicates({})
        self.assertIn("groups", result)
        self.assertIn("total_duplicates", result)
        if result["groups"]:
            group = result["groups"][0]
            self.assertIn("items", group)
            self.assertIn("similarity", group)


class GetTextFieldsTestCase(unittest.TestCase):
    """_get_text — извлечение текста из разных полей записи."""

    def test_text_field_used(self) -> None:
        item = {"text": "hello"}
        self.assertEqual(DuplicateDetector._get_text(item), "hello")

    def test_transcript_field_fallback(self) -> None:
        item = {"transcript": "transcript text"}
        self.assertEqual(DuplicateDetector._get_text(item), "transcript text")

    def test_empty_item_returns_empty_string(self) -> None:
        self.assertEqual(DuplicateDetector._get_text({}), "")

    def test_text_field_takes_priority_over_transcript(self) -> None:
        item = {"text": "primary", "transcript": "secondary"}
        self.assertEqual(DuplicateDetector._get_text(item), "primary")


class GetTimestampTestCase(unittest.TestCase):
    """_get_timestamp — разные форматы временных меток."""

    def test_float_ts_returned_as_float(self) -> None:
        item = {"ts": 1_700_000_000.0}
        self.assertEqual(DuplicateDetector._get_timestamp(item), 1_700_000_000.0)

    def test_int_ts_returned_as_float(self) -> None:
        item = {"ts": 1_700_000_000}
        result = DuplicateDetector._get_timestamp(item)
        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 1_700_000_000.0)

    def test_iso_string_ts_parsed(self) -> None:
        item = {"created_at": "2024-01-15T10:00:00+00:00"}
        result = DuplicateDetector._get_timestamp(item)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, float)
        self.assertGreater(result, 0)

    def test_missing_ts_returns_none(self) -> None:
        self.assertIsNone(DuplicateDetector._get_timestamp({}))

    def test_invalid_string_ts_returns_none(self) -> None:
        item = {"ts": "not-a-date"}
        self.assertIsNone(DuplicateDetector._get_timestamp(item))

    def test_timestamp_field_alias(self) -> None:
        item = {"timestamp": 1_600_000_000.5}
        result = DuplicateDetector._get_timestamp(item)
        self.assertAlmostEqual(result, 1_600_000_000.5)


class FindDuplicatesExtraTestCase(unittest.TestCase):
    """find_duplicates — дополнительные граничные случаи."""

    def setUp(self) -> None:
        self.det = DuplicateDetector()
        self.base_ts = 1_700_000_000.0

    def test_items_with_empty_text_skipped(self) -> None:
        items = [
            {"text": "", "ts": self.base_ts},
            {"text": "real content here", "ts": self.base_ts + 2},
            {"text": "real content here", "ts": self.base_ts + 4},
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].items), 2)

    def test_low_threshold_finds_partial_matches(self) -> None:
        items = [
            _make_item("hello world today", self.base_ts),
            _make_item("hello earth tomorrow", self.base_ts + 5),
        ]
        groups_strict = self.det.find_duplicates(items, similarity_threshold=0.95)
        groups_loose = self.det.find_duplicates(items, similarity_threshold=0.3)
        self.assertEqual(len(groups_strict), 0)
        self.assertEqual(len(groups_loose), 1)

    def test_similarity_score_is_between_zero_and_one(self) -> None:
        items = [
            _make_item("The quick brown fox", self.base_ts),
            _make_item("The quick brown fix", self.base_ts + 1),
        ]
        groups = self.det.find_duplicates(items, similarity_threshold=0.8)
        if groups:
            self.assertGreater(groups[0].similarity, 0.0)
            self.assertLessEqual(groups[0].similarity, 1.0)

    def test_transcript_field_recognised_for_dedup(self) -> None:
        items = [
            {"transcript": "same transcript text", "ts": self.base_ts},
            {"transcript": "same transcript text", "ts": self.base_ts + 3},
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 1)

    def test_exact_time_boundary_included(self) -> None:
        """Записи ровно в 60-секундном окне должны проверяться."""
        text = "boundary test sentence"
        items = [
            _make_item(text, self.base_ts),
            _make_item(text, self.base_ts + 60),  # exactly at boundary
        ]
        groups = self.det.find_duplicates(items)
        # abs(60 - 0) == 60 which is NOT > 60, so they should be compared
        self.assertEqual(len(groups), 1)


class TestWave117DuplicateDetector(unittest.TestCase):
    """Wave 117 required test cases for DuplicateDetector."""

    def setUp(self) -> None:
        self.det = DuplicateDetector()
        self.base_ts = 1_700_000_000.0

    # test_identical_text_is_duplicate
    def test_identical_text_is_duplicate(self) -> None:
        """Одинаковые тексты — дубликат при любом разумном пороге."""
        self.assertTrue(self.det.is_duplicate("Привет мир", "Привет мир"))
        self.assertTrue(self.det.is_duplicate("Hello World", "Hello World"))
        self.assertTrue(self.det.is_duplicate("Hola mundo", "Hola mundo"))

    # test_dissimilar_text_not_duplicate
    def test_dissimilar_text_not_duplicate(self) -> None:
        """Полностью разные тексты не являются дубликатами."""
        self.assertFalse(self.det.is_duplicate(
            "Архитектура бэкенда",
            "Планирование спринта команды",
        ))
        self.assertFalse(self.det.is_duplicate(
            "The quick brown fox",
            "Completely unrelated sentence here",
        ))

    # test_near_duplicate_above_threshold
    def test_near_duplicate_above_threshold(self) -> None:
        """Почти идентичный текст (1 слово отличается) выше порога 0.9."""
        text1 = "Обсуждаем архитектуру нового сервиса для обработки данных"
        text2 = "Обсуждаем архитектуру нового сервиса для хранения данных"
        # Should be above 0.85 threshold (one word differs in a long sentence)
        self.assertTrue(self.det.is_duplicate(text1, text2, threshold=0.85))

    # test_unicode_text_compared
    def test_unicode_text_compared(self) -> None:
        """Unicode тексты (кириллица, испанский) корректно сравниваются."""
        # Identical cyrillic
        self.assertTrue(self.det.is_duplicate(
            "Привет это тест на кириллице",
            "Привет это тест на кириллице",
        ))
        # Identical Spanish with accents
        self.assertTrue(self.det.is_duplicate(
            "Hola esto es una prueba en español",
            "Hola esto es una prueba en español",
        ))
        # Different unicode texts
        self.assertFalse(self.det.is_duplicate(
            "Привет мир",
            "Hola mundo completamente diferente aqui",
            threshold=0.9,
        ))
        # Ensure find_duplicates also works with unicode items
        items = [
            {"text": "Кириллица тест запись первая", "ts": self.base_ts},
            {"text": "Кириллица тест запись первая", "ts": self.base_ts + 5},
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 1)

    # test_empty_strings
    def test_empty_strings(self) -> None:
        """Пустые строки никогда не являются дубликатами."""
        self.assertFalse(self.det.is_duplicate("", ""))
        self.assertFalse(self.det.is_duplicate("", "hello world"))
        self.assertFalse(self.det.is_duplicate("hello world", ""))
        # Empty items in find_duplicates are skipped
        items = [
            {"text": "", "ts": self.base_ts},
            {"text": "", "ts": self.base_ts + 2},
            {"text": "real content", "ts": self.base_ts + 4},
        ]
        groups = self.det.find_duplicates(items)
        self.assertEqual(len(groups), 0)

    # test_threshold_adjustable
    def test_threshold_adjustable(self) -> None:
        """Порог сходства влияет на результат: ниже → больше совпадений."""
        text1 = "hello world today"
        text2 = "hello world tonight"
        # Very loose threshold — must match
        result_loose = self.det.is_duplicate(text1, text2, threshold=0.5)
        self.assertTrue(result_loose, "Should match at threshold=0.5")
        # Verify find_duplicates also respects threshold
        items = [
            _make_item(text1, self.base_ts),
            _make_item(text2, self.base_ts + 5),
        ]
        groups_strict = self.det.find_duplicates(items, similarity_threshold=0.99)
        groups_loose = self.det.find_duplicates(items, similarity_threshold=0.5)
        self.assertLessEqual(len(groups_strict), len(groups_loose))

    # test_concurrent_compare
    def test_concurrent_compare(self) -> None:
        """DuplicateDetector безопасен при одновременном вызове is_duplicate из нескольких потоков."""
        import threading

        pairs = [
            ("hello world", "hello world"),
            ("foo bar baz", "totally different"),
            ("Привет мир", "Привет мир"),
            ("The quick brown fox", "The quick brown fox jumps"),
            ("unique text one", "unique text one"),
        ]
        results: list[bool] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(t1: str, t2: str) -> None:
            try:
                res = self.det.is_duplicate(t1, t2)
                with lock:
                    results.append(res)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t1, t2))
            for t1, t2 in pairs * 5
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(results), len(threads))
        # Sanity: identical pairs should be True
        self.assertTrue(self.det.is_duplicate("hello world", "hello world"))


if __name__ == "__main__":
    unittest.main()
