"""Тесты HistoryService — изолированного сервиса истории Krab Ear.

Файл написан заранее (TDD): HistoryService ещё не выделен в отдельный модуль,
поэтому тесты пропускаются через @skipIf до появления backend/history_service.py.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore

try:
    from backend.history_service import HistoryService
except ImportError:
    HistoryService = None  # type: ignore[assignment,misc]


@unittest.skipIf(HistoryService is None, "HistoryService not yet extracted")
class HistoryServiceTestCase(unittest.TestCase):
    """Проверяет публичный API HistoryService через IPC-подобные вызовы."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    # ------------------------------------------------------------------
    # 1. get_history_page — пустое хранилище
    # ------------------------------------------------------------------

    def test_get_history_page_empty(self) -> None:
        """Пустое хранилище возвращает пустой список и next_cursor=None."""
        result = self.svc.handle_get_history_page({})
        self.assertIn("items", result)
        self.assertIn("next_cursor", result)
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["next_cursor"])

    # ------------------------------------------------------------------
    # 2. add item, then get page
    # ------------------------------------------------------------------

    def test_add_and_get_history(self) -> None:
        """Добавленная запись возвращается в первой странице истории."""
        item = self.store.add_history_item(text="привет мир", paste_status="ok")
        result = self.svc.handle_get_history_page({"limit": 10})
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], item.id)
        self.assertEqual(result["items"][0]["text"], "привет мир")
        self.assertIsNone(result["next_cursor"])

    def test_get_history_page_respects_limit_and_cursor(self) -> None:
        """Пагинация работает корректно: limit и next_cursor."""
        for i in range(5):
            self.store.add_history_item(text=f"запись-{i}", paste_status="ok")

        page1 = self.svc.handle_get_history_page({"limit": 3})
        self.assertEqual(len(page1["items"]), 3)
        self.assertIsNotNone(page1["next_cursor"])

        page2 = self.svc.handle_get_history_page(
            {"limit": 3, "cursor": page1["next_cursor"]}
        )
        self.assertEqual(len(page2["items"]), 2)
        self.assertIsNone(page2["next_cursor"])

        # Все id уникальны и не пересекаются
        ids1 = {i["id"] for i in page1["items"]}
        ids2 = {i["id"] for i in page2["items"]}
        self.assertEqual(len(ids1 & ids2), 0)

    # ------------------------------------------------------------------
    # 3. delete_history_item
    # ------------------------------------------------------------------

    def test_delete_history_item(self) -> None:
        """Удалённая запись не появляется в следующей странице."""
        item = self.store.add_history_item(text="удалить меня", paste_status="ok")
        other = self.store.add_history_item(text="оставить", paste_status="ok")

        result = self.svc.handle_delete_history_item({"id": item.id})
        self.assertEqual(result, {"deleted": True})

        page = self.svc.handle_get_history_page({"limit": 50})
        ids = [i["id"] for i in page["items"]]
        self.assertNotIn(item.id, ids)
        self.assertIn(other.id, ids)

    def test_delete_history_item_missing_id_raises(self) -> None:
        """Вызов без id должен поднять KeyError или ValueError."""
        with self.assertRaises((KeyError, ValueError)):
            self.svc.handle_delete_history_item({})

    # ------------------------------------------------------------------
    # 4. compact_history
    # ------------------------------------------------------------------

    def test_compact_history(self) -> None:
        """Компактация после удалений возвращает флаг compacted=True и статистику."""
        ids = []
        for i in range(10):
            item = self.store.add_history_item(text=f"item-{i}", paste_status="ok")
            ids.append(item.id)

        # Удаляем половину
        for item_id in ids[:5]:
            self.store.delete_history_item(item_id)

        result = self.svc.handle_compact_history({})
        self.assertTrue(result.get("compacted"))
        # Ожидаем хотя бы базовые поля статистики
        for key in ("before_total_bytes", "after_total_bytes", "reclaimed_bytes"):
            self.assertIn(key, result, f"Ожидалось поле {key!r} в ответе compact")

        # После компактации живые записи на месте
        page = self.svc.handle_get_history_page({"limit": 50})
        self.assertEqual(len(page["items"]), 5)

    # ------------------------------------------------------------------
    # 5. get_history_stats
    # ------------------------------------------------------------------

    def test_get_history_stats(self) -> None:
        """handle_get_history_stats возвращает ожидаемую структуру."""
        self.store.add_history_item(text="тест статистики", paste_status="ok")

        stats = self.svc.handle_get_history_stats({})
        for key in ("active_count", "total_bytes"):
            self.assertIn(key, stats, f"Ожидалось поле {key!r} в stats")
        self.assertGreaterEqual(stats["active_count"], 1)
        self.assertGreater(stats["total_bytes"], 0)

    def test_get_history_stats_empty(self) -> None:
        """Пустое хранилище возвращает нулевые счётчики."""
        stats = self.svc.handle_get_history_stats({})
        self.assertEqual(stats["active_count"], 0)

    # ------------------------------------------------------------------
    # 6. get_storage_info
    # ------------------------------------------------------------------

    def test_get_storage_info(self) -> None:
        """handle_get_storage_info возвращает размеры файлов истории."""
        self.store.add_history_item(text="размер файла", paste_status="ok")

        info = self.svc.handle_get_storage_info({})
        # Ожидаемые поля: history_bytes, settings_bytes, total_bytes
        for key in ("history_bytes", "total_bytes"):
            self.assertIn(key, info, f"Ожидалось поле {key!r} в storage_info")
        self.assertGreater(info["history_bytes"], 0)
        self.assertGreater(info["total_bytes"], 0)

    def test_get_storage_info_empty_store(self) -> None:
        """get_storage_info работает и при пустом хранилище (файлы могут отсутствовать)."""
        info = self.svc.handle_get_storage_info({})
        self.assertIn("total_bytes", info)
        self.assertIsInstance(info["total_bytes"], int)

    # ------------------------------------------------------------------
    # 7. cleanup_old_history
    # ------------------------------------------------------------------

    def test_cleanup_old_history(self) -> None:
        """Старые записи удаляются, новые остаются."""
        import json as _json

        # Добавляем старые записи напрямую (ts в прошлом)
        old_items = [
            {
                "id": f"old-{i}",
                "ts": "2020-01-01T00:00:00",
                "text": f"старая запись {i}",
                "paste_status": "ok",
            }
            for i in range(3)
        ]
        with self.store.history_path.open("w", encoding="utf-8") as fh:
            for entry in old_items:
                fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")

        # Добавляем свежую запись через store
        fresh = self.store.add_history_item(text="свежая запись", paste_status="ok")

        # Удаляем всё старше 30 дней
        result = self.svc.handle_cleanup_old_history({"older_than_days": 30})
        self.assertIn("deleted_count", result)
        self.assertIsInstance(result["deleted_count"], int)
        self.assertGreaterEqual(result["deleted_count"], 3)

        # Свежая запись должна остаться
        page = self.svc.handle_get_history_page({"limit": 50})
        ids = [i["id"] for i in page["items"]]
        self.assertIn(fresh.id, ids)
        # Старые — удалены
        for old in old_items:
            self.assertNotIn(old["id"], ids)

    def test_cleanup_old_history_no_old_items(self) -> None:
        """Если старых записей нет, deleted_count == 0."""
        self.store.add_history_item(text="новая", paste_status="ok")
        result = self.svc.handle_cleanup_old_history({"older_than_days": 30})
        self.assertEqual(result["deleted_count"], 0)

    def test_cleanup_old_history_default_params(self) -> None:
        """Вызов без параметров не должен падать (используются дефолтные значения)."""
        result = self.svc.handle_cleanup_old_history({})
        self.assertIn("deleted_count", result)


if __name__ == "__main__":
    unittest.main()
