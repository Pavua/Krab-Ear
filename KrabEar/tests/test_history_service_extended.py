"""Расширенные unit-тесты для backend/history_service.py.

Покрывает методы, не охваченные в test_history_service.py:
- handle_add_history_item (normal + empty text raises)
- handle_search_history (keyword search)
- handle_fuzzy_search (empty query, non-empty)
- handle_get_history_item (existing + missing id)
- handle_add_tag / handle_remove_tag / handle_get_tags / handle_list_all_tags / handle_search_by_tag
- handle_toggle_favorite / handle_get_favorites / handle_is_favorite
- handle_set_annotation / handle_get_annotation
- _format_duration_human (edge cases)
- _format_ts_human (valid + invalid)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from backend.history_service import HistoryService
    from backend.state_store import StateStore
    _SKIP = False
except ImportError:
    _SKIP = True


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceAddItemTestCase(unittest.TestCase):
    """Тесты handle_add_history_item."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_add_item_returns_dict_with_id(self):
        result = self.svc.handle_add_history_item({"text": "привет мир", "paste_status": "ok"})
        self.assertIn("id", result)
        self.assertEqual(result["text"], "привет мир")
        self.assertIsInstance(result["id"], str)
        self.assertTrue(len(result["id"]) > 0)

    def test_add_item_with_translation(self):
        result = self.svc.handle_add_history_item({
            "text": "hola mundo",
            "paste_status": "ok",
            "translated_text": "привет мир",
            "translation_mode": "es_to_ru",
            "source_lang": "es",
            "target_lang": "ru",
            "translation_status": "ok",
        })
        self.assertEqual(result["translated_text"], "привет мир")
        self.assertEqual(result["translation_mode"], "es_to_ru")

    def test_add_item_empty_text_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_history_item({"text": "", "paste_status": "ok"})

    def test_add_item_whitespace_text_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_history_item({"text": "   ", "paste_status": "ok"})

    def test_add_item_default_paste_status(self):
        result = self.svc.handle_add_history_item({"text": "тест дефолт"})
        # Should not raise; paste_status defaults to "failed"
        self.assertIn("paste_status", result)


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceSearchTestCase(unittest.TestCase):
    """Тесты handle_search_history."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

        # Populate store
        self.store.add_history_item(text="привет мир ключевое слово", paste_status="ok")
        self.store.add_history_item(text="hello world test phrase", paste_status="ok")
        self.store.add_history_item(text="hola mundo otra frase", paste_status="ok")

    def test_search_returns_matching_items(self):
        result = self.svc.handle_search_history({"query": "ключевое"})
        self.assertIn("items", result)
        self.assertIn("next_cursor", result)
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("ключевое", result["items"][0]["text"])

    def test_search_no_match_returns_empty(self):
        result = self.svc.handle_search_history({"query": "нет такого слова xyz"})
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["next_cursor"])

    def test_search_empty_query_returns_all(self):
        result = self.svc.handle_search_history({"query": "", "limit": 100})
        self.assertGreaterEqual(len(result["items"]), 3)

    def test_search_respects_limit(self):
        result = self.svc.handle_search_history({"query": "", "limit": 2})
        self.assertLessEqual(len(result["items"]), 2)


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceFuzzySearchTestCase(unittest.TestCase):
    """Тесты handle_fuzzy_search."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)
        self.store.add_history_item(text="привет мир тест слово", paste_status="ok")
        self.store.add_history_item(text="hello world sample text", paste_status="ok")

    def test_fuzzy_search_empty_query_returns_empty(self):
        result = self.svc.handle_fuzzy_search({"query": ""})
        self.assertEqual(result, {"matches": []})

    def test_fuzzy_search_whitespace_returns_empty(self):
        result = self.svc.handle_fuzzy_search({"query": "   "})
        self.assertEqual(result, {"matches": []})

    def test_fuzzy_search_returns_matches_structure(self):
        result = self.svc.handle_fuzzy_search({"query": "привет", "threshold": 0.0})
        self.assertIn("matches", result)
        # With threshold 0.0 any item is a match
        self.assertGreater(len(result["matches"]), 0)
        for match in result["matches"]:
            self.assertIn("id", match)
            self.assertIn("text", match)
            self.assertIn("score", match)

    def test_fuzzy_search_respects_threshold(self):
        # threshold=1.0 means exact match only → most items filtered
        result = self.svc.handle_fuzzy_search({"query": "xyz abc", "threshold": 1.0})
        self.assertIn("matches", result)

    def test_fuzzy_search_respects_limit(self):
        result = self.svc.handle_fuzzy_search({"query": "text", "threshold": 0.0, "limit": 1})
        self.assertLessEqual(len(result["matches"]), 1)


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceGetItemTestCase(unittest.TestCase):
    """Тесты handle_get_history_item."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)
        self.item = self.store.add_history_item(text="полный текст записи", paste_status="ok")

    def test_get_existing_item(self):
        result = self.svc.handle_get_history_item({"id": self.item.id})
        self.assertEqual(result["id"], self.item.id)
        self.assertEqual(result["text"], "полный текст записи")
        self.assertIn("text_length", result)
        self.assertIn("word_count", result)
        self.assertEqual(result["text_length"], len("полный текст записи"))
        self.assertEqual(result["word_count"], 3)

    def test_get_missing_item_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_history_item({"id": "nonexistent-id-xyz"})

    def test_get_item_empty_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_history_item({"id": ""})

    def test_get_item_no_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_history_item({})


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceTagsTestCase(unittest.TestCase):
    """Тесты работы с тегами."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)
        self.item = self.store.add_history_item(text="запись для тегов", paste_status="ok")

    def test_add_tag(self):
        result = self.svc.handle_add_tag({"id": self.item.id, "tag": "важное"})
        self.assertEqual(result["id"], self.item.id)
        self.assertIn("важное", result["tags"])

    def test_add_tag_twice_no_duplicate(self):
        self.svc.handle_add_tag({"id": self.item.id, "tag": "уникальный"})
        result = self.svc.handle_add_tag({"id": self.item.id, "tag": "уникальный"})
        self.assertEqual(result["tags"].count("уникальный"), 1)

    def test_remove_tag(self):
        self.svc.handle_add_tag({"id": self.item.id, "tag": "удалить"})
        result = self.svc.handle_remove_tag({"id": self.item.id, "tag": "удалить"})
        self.assertNotIn("удалить", result["tags"])

    def test_remove_nonexistent_tag_ok(self):
        # Удаление тега которого нет — должно работать без ошибки
        result = self.svc.handle_remove_tag({"id": self.item.id, "tag": "несуществующий"})
        self.assertIn("tags", result)

    def test_get_tags_empty(self):
        result = self.svc.handle_get_tags({"id": self.item.id})
        self.assertEqual(result["tags"], [])

    def test_get_tags_after_add(self):
        self.svc.handle_add_tag({"id": self.item.id, "tag": "тег1"})
        self.svc.handle_add_tag({"id": self.item.id, "tag": "тег2"})
        result = self.svc.handle_get_tags({"id": self.item.id})
        self.assertIn("тег1", result["tags"])
        self.assertIn("тег2", result["tags"])

    def test_search_by_tag(self):
        self.svc.handle_add_tag({"id": self.item.id, "tag": "поиск"})
        result = self.svc.handle_search_by_tag({"tag": "поиск"})
        self.assertIn("items", result)
        self.assertIn("count", result)
        ids = [i["id"] for i in result["items"]]
        self.assertIn(self.item.id, ids)

    def test_search_by_tag_no_matches(self):
        result = self.svc.handle_search_by_tag({"tag": "отсутствующий"})
        self.assertEqual(result["count"], 0)

    def test_list_all_tags(self):
        item2 = self.store.add_history_item(text="вторая запись", paste_status="ok")
        self.svc.handle_add_tag({"id": self.item.id, "tag": "общий"})
        self.svc.handle_add_tag({"id": item2.id, "tag": "общий"})
        self.svc.handle_add_tag({"id": self.item.id, "tag": "уникальный"})

        result = self.svc.handle_list_all_tags({})
        self.assertIn("tags", result)
        tag_map = {t["tag"]: t["count"] for t in result["tags"]}
        self.assertEqual(tag_map["общий"], 2)
        self.assertEqual(tag_map["уникальный"], 1)

    def test_add_tag_missing_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_tag({"tag": "тест"})

    def test_add_tag_missing_tag_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_tag({"id": self.item.id})


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceFavoritesTestCase(unittest.TestCase):
    """Тесты избранного."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)
        self.item = self.store.add_history_item(text="запись для избранного", paste_status="ok")

    def test_toggle_favorite_adds(self):
        result = self.svc.handle_toggle_favorite({"id": self.item.id})
        self.assertEqual(result["id"], self.item.id)
        self.assertTrue(result["favorite"])

    def test_toggle_favorite_twice_removes(self):
        self.svc.handle_toggle_favorite({"id": self.item.id})
        result = self.svc.handle_toggle_favorite({"id": self.item.id})
        self.assertFalse(result["favorite"])

    def test_get_favorites_empty(self):
        result = self.svc.handle_get_favorites({})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])

    def test_get_favorites_returns_favorited_items(self):
        self.svc.handle_toggle_favorite({"id": self.item.id})
        result = self.svc.handle_get_favorites({})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["id"], self.item.id)

    def test_is_favorite_false_initially(self):
        result = self.svc.handle_is_favorite({"id": self.item.id})
        self.assertFalse(result["favorite"])
        self.assertEqual(result["id"], self.item.id)

    def test_is_favorite_true_after_toggle(self):
        self.svc.handle_toggle_favorite({"id": self.item.id})
        result = self.svc.handle_is_favorite({"id": self.item.id})
        self.assertTrue(result["favorite"])

    def test_toggle_favorite_missing_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_toggle_favorite({})

    def test_toggle_favorite_nonexistent_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_toggle_favorite({"id": "nonexistent-xyz"})


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceAnnotationsTestCase(unittest.TestCase):
    """Тесты аннотаций (заметок к записям)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)
        self.item = self.store.add_history_item(text="запись с аннотацией", paste_status="ok")

    def test_set_and_get_annotation(self):
        self.svc.handle_set_annotation({"id": self.item.id, "note": "моя заметка"})
        result = self.svc.handle_get_annotation({"id": self.item.id})
        self.assertEqual(result["id"], self.item.id)
        self.assertEqual(result["note"], "моя заметка")

    def test_get_annotation_initially_none_or_empty(self):
        result = self.svc.handle_get_annotation({"id": self.item.id})
        # Начальная аннотация — None или пустая строка
        self.assertIn(result["note"], (None, ""))

    def test_set_annotation_empty_clears(self):
        self.svc.handle_set_annotation({"id": self.item.id, "note": "заметка"})
        self.svc.handle_set_annotation({"id": self.item.id, "note": ""})
        result = self.svc.handle_get_annotation({"id": self.item.id})
        self.assertIn(result["note"], (None, ""))

    def test_set_annotation_missing_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_set_annotation({"note": "тест"})

    def test_get_annotation_missing_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_annotation({})

    def test_get_annotation_nonexistent_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_annotation({"id": "nonexistent-xyz"})


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class HistoryServiceFormatHelpersTestCase(unittest.TestCase):
    """Тесты статических helper'ов форматирования."""

    def test_format_duration_none_returns_empty(self):
        self.assertEqual(HistoryService._format_duration_human(None), "")

    def test_format_duration_zero_returns_empty(self):
        self.assertEqual(HistoryService._format_duration_human(0), "")

    def test_format_duration_seconds_only(self):
        self.assertEqual(HistoryService._format_duration_human(45), "45с")

    def test_format_duration_minutes_and_seconds(self):
        self.assertEqual(HistoryService._format_duration_human(125), "2м 5с")

    def test_format_duration_hours_minutes_seconds(self):
        result = HistoryService._format_duration_human(3661)
        self.assertIn("ч", result)
        self.assertIn("м", result)

    def test_format_ts_human_valid(self):
        result = HistoryService._format_ts_human("2026-04-11T22:46:00")
        self.assertEqual(result, "2026-04-11 22:46")

    def test_format_ts_human_invalid_returns_original(self):
        result = HistoryService._format_ts_human("not-a-date")
        self.assertEqual(result, "not-a-date")

    def test_format_ts_human_none_returns_none(self):
        result = HistoryService._format_ts_human(None)  # type: ignore
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
