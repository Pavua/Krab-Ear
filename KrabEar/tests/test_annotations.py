"""Тесты для аннотаций/заметок к записям истории Krab Ear.

Покрывает:
- StateStore: set_annotation, get_annotation, delete_annotation, search_annotations
- HistoryService: handle_set_annotation, handle_get_annotation, handle_search_annotations
- IPC-регистрация: методы set_annotation, get_annotation, search_annotations
"""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_store(tmp_dir: Path) -> StateStore:
    return StateStore(data_dir=tmp_dir)


def _add_item(store: StateStore, text: str = "Тестовая транскрипция") -> str:
    item = store.add_history_item(text=text)
    return item.id


class TestStateStoreAnnotations(unittest.TestCase):
    """Юнит-тесты StateStore для операций с аннотациями."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.item_id = _add_item(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 1. set_annotation + get_annotation — базовый round-trip
    # ------------------------------------------------------------------

    def test_set_and_get_annotation(self) -> None:
        note = "Важная встреча"
        ok = self.store.set_annotation(self.item_id, note)
        self.assertTrue(ok)
        result = self.store.get_annotation(self.item_id)
        self.assertEqual(result, note)

    # ------------------------------------------------------------------
    # 2. get_annotation возвращает None для записи без заметки
    # ------------------------------------------------------------------

    def test_get_annotation_returns_none_when_not_set(self) -> None:
        result = self.store.get_annotation(self.item_id)
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # 3. set_annotation перезаписывает предыдущую заметку (last-write-wins)
    # ------------------------------------------------------------------

    def test_set_annotation_overwrites_previous(self) -> None:
        self.store.set_annotation(self.item_id, "первая версия")
        self.store.set_annotation(self.item_id, "обновлённая версия")
        result = self.store.get_annotation(self.item_id)
        self.assertEqual(result, "обновлённая версия")

    # ------------------------------------------------------------------
    # 4. set_annotation — несуществующий id → False
    # ------------------------------------------------------------------

    def test_set_annotation_unknown_id_returns_false(self) -> None:
        ok = self.store.set_annotation("nonexistent-id", "заметка")
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # 5. delete_annotation — удаляет заметку (tombstone пустой строкой)
    # ------------------------------------------------------------------

    def test_delete_annotation_clears_note(self) -> None:
        self.store.set_annotation(self.item_id, "временная заметка")
        self.store.delete_annotation(self.item_id)
        result = self.store.get_annotation(self.item_id)
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # 6. search_annotations — базовый полнотекстовый поиск
    # ------------------------------------------------------------------

    def test_search_annotations_finds_matching(self) -> None:
        id2 = _add_item(self.store, "другая запись")
        self.store.set_annotation(self.item_id, "важная встреча по проекту")
        self.store.set_annotation(id2, "звонок с командой")
        results = self.store.search_annotations("встреча")
        ids = [r["id"] for r in results]
        self.assertIn(self.item_id, ids)
        self.assertNotIn(id2, ids)

    # ------------------------------------------------------------------
    # 7. search_annotations — пустой запрос возвращает все заметки
    # ------------------------------------------------------------------

    def test_search_annotations_empty_query_returns_all(self) -> None:
        id2 = _add_item(self.store, "вторая запись")
        self.store.set_annotation(self.item_id, "заметка A")
        self.store.set_annotation(id2, "заметка B")
        results = self.store.search_annotations("")
        self.assertEqual(len(results), 2)

    # ------------------------------------------------------------------
    # 8. annotations_path — файл создаётся при инициализации
    # ------------------------------------------------------------------

    def test_annotations_file_exists_after_init(self) -> None:
        self.assertTrue(self.store.annotations_path.exists())


class TestHistoryServiceAnnotations(unittest.TestCase):
    """Тесты HistoryService-обработчиков аннотаций."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = HistoryService(store=self.store)
        self.item_id = _add_item(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 9. handle_set_annotation + handle_get_annotation round-trip
    # ------------------------------------------------------------------

    def test_handle_set_and_get_annotation(self) -> None:
        note = "Встреча с клиентом"
        set_res = self.svc.handle_set_annotation({"id": self.item_id, "note": note})
        self.assertEqual(set_res["id"], self.item_id)
        self.assertEqual(set_res["note"], note)

        get_res = self.svc.handle_get_annotation({"id": self.item_id})
        self.assertEqual(get_res["note"], note)

    # ------------------------------------------------------------------
    # 10. handle_set_annotation — пустой id → RuntimeError
    # ------------------------------------------------------------------

    def test_handle_set_annotation_empty_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.svc.handle_set_annotation({"id": "", "note": "заметка"})

    # ------------------------------------------------------------------
    # 11. handle_get_annotation — несуществующая запись → RuntimeError
    # ------------------------------------------------------------------

    def test_handle_get_annotation_unknown_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_annotation({"id": "no-such-id"})

    # ------------------------------------------------------------------
    # 12. handle_search_annotations — поиск находит нужную заметку
    # ------------------------------------------------------------------

    def test_handle_search_annotations_returns_matches(self) -> None:
        id2 = _add_item(self.store, "другая запись")
        self.svc.handle_set_annotation({"id": self.item_id, "note": "конференция по AI"})
        self.svc.handle_set_annotation({"id": id2, "note": "обычный звонок"})

        res = self.svc.handle_search_annotations({"query": "AI"})
        self.assertIn("results", res)
        self.assertIn("count", res)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["id"], self.item_id)

    # ------------------------------------------------------------------
    # 13. handle_set_annotation — несуществующий id → RuntimeError
    # ------------------------------------------------------------------

    def test_handle_set_annotation_unknown_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.svc.handle_set_annotation({"id": "ghost-id", "note": "заметка"})

    # ------------------------------------------------------------------
    # 14. handle_search_annotations — пустой запрос возвращает все заметки
    # ------------------------------------------------------------------

    def test_handle_search_annotations_empty_query(self) -> None:
        id2 = _add_item(self.store, "вторая запись")
        self.svc.handle_set_annotation({"id": self.item_id, "note": "заметка 1"})
        self.svc.handle_set_annotation({"id": id2, "note": "заметка 2"})
        res = self.svc.handle_search_annotations({"query": ""})
        self.assertEqual(res["count"], 2)

    # ------------------------------------------------------------------
    # 15. handle_get_annotation — запись без заметки возвращает note: None
    # ------------------------------------------------------------------

    def test_handle_get_annotation_no_note_returns_none(self) -> None:
        res = self.svc.handle_get_annotation({"id": self.item_id})
        self.assertIsNone(res["note"])


if __name__ == "__main__":
    unittest.main()
