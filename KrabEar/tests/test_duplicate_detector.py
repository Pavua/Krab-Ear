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


if __name__ == "__main__":
    unittest.main()
