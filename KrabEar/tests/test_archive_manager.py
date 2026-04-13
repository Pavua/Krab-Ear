"""Unit-тесты для ArchiveManager."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager, ArchiveResult


# ---------------------------------------------------------------------------
# Фейковые вспомогательные объекты
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    """Минимальный фейк HistoryItem для тестов ArchiveManager."""

    def __init__(self, item_id: str, text: str, ts: str = "2026-01-01T10:00:00") -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.paste_status = "ok"
        self.source_text = ""
        self.translated_text = ""
        self.translation_mode = "off"
        self.source_lang = ""
        self.target_lang = ""
        self.translation_status = "not_requested"
        self.translation_engine = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "text": self.text,
            "paste_status": self.paste_status,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "translation_mode": self.translation_mode,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "translation_status": self.translation_status,
            "translation_engine": self.translation_engine,
        }


class FakeStore:
    """Минимальный фейк StateStore для тестов ArchiveManager."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: set[str] = set()
        self._added: list[dict[str, Any]] = []

    def add_fake_item(self, item_id: str, text: str, ts: str = "2026-01-01T10:00:00") -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text, ts=ts)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        if item_id in self._deleted:
            return None
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.add(item_id)
            return True
        return False

    def add_history_item(
        self,
        text: str,
        paste_status: str = "failed",
        source_text: str = "",
        translated_text: str = "",
        translation_mode: str = "off",
        source_lang: str = "",
        target_lang: str = "",
        translation_status: str = "not_requested",
        translation_engine: str = "",
    ) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id="restored-" + text[:8], text=text)
        self._items[item.id] = item
        self._added.append({"text": text, "paste_status": paste_status})
        return item


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class ArchiveManagerBasicTestCase(unittest.TestCase):
    """Базовые тесты архивирования и статистики."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def test_archive_dir_created(self) -> None:
        """Директория archive создаётся при инициализации."""
        archive_dir = Path(self._tmpdir) / "archive"
        self.assertTrue(archive_dir.exists())

    def test_archive_file_created(self) -> None:
        """Файл archive.ndjson создаётся при инициализации."""
        archive_file = Path(self._tmpdir) / "archive" / "archive.ndjson"
        self.assertTrue(archive_file.exists())

    def test_archive_items_returns_archive_result(self) -> None:
        """archive_items возвращает ArchiveResult."""
        self._store.add_fake_item("id-1", "Тестовая запись")
        result = self._mgr.archive_items(item_ids=["id-1"])
        self.assertIsInstance(result, ArchiveResult)
        self.assertEqual(result.archived_count, 1)

    def test_archive_items_count(self) -> None:
        """Архивирование нескольких записей: корректный счётчик."""
        self._store.add_fake_item("a1", "Запись 1")
        self._store.add_fake_item("a2", "Запись 2")
        self._store.add_fake_item("a3", "Запись 3")
        result = self._mgr.archive_items(item_ids=["a1", "a2", "a3"])
        self.assertEqual(result.archived_count, 3)

    def test_archive_items_removes_from_active(self) -> None:
        """После архивирования запись недоступна в активной истории."""
        self._store.add_fake_item("del-1", "Удалить меня")
        self._mgr.archive_items(item_ids=["del-1"])
        # FakeStore.delete_history_item помечает как удалённый
        self.assertIn("del-1", self._store._deleted)

    def test_archive_items_empty_list(self) -> None:
        """Пустой список — 0 архивировано."""
        result = self._mgr.archive_items(item_ids=[])
        self.assertEqual(result.archived_count, 0)

    def test_archive_items_nonexistent_id(self) -> None:
        """Несуществующие ID просто игнорируются."""
        result = self._mgr.archive_items(item_ids=["nonexistent-123"])
        self.assertEqual(result.archived_count, 0)

    def test_archive_items_written_to_file(self) -> None:
        """Архивированные записи записываются в archive.ndjson."""
        self._store.add_fake_item("write-1", "Запись для файла")
        self._mgr.archive_items(item_ids=["write-1"])
        archive_file = Path(self._tmpdir) / "archive" / "archive.ndjson"
        lines = [l for l in archive_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["id"], "write-1")

    def test_archived_item_has_archived_at_field(self) -> None:
        """Архивированные записи содержат поле archived_at."""
        self._store.add_fake_item("ts-1", "Запись с временем")
        self._mgr.archive_items(item_ids=["ts-1"])
        items = self._mgr.list_archived()
        self.assertEqual(len(items), 1)
        self.assertIn("archived_at", items[0])


class ArchiveManagerListTestCase(unittest.TestCase):
    """Тесты list_archived."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def test_list_archived_empty(self) -> None:
        """Пустой архив — пустой список."""
        result = self._mgr.list_archived()
        self.assertEqual(result, [])

    def test_list_archived_after_archiving(self) -> None:
        """После архивирования list_archived возвращает записи."""
        self._store.add_fake_item("list-1", "Список запись 1")
        self._store.add_fake_item("list-2", "Список запись 2")
        self._mgr.archive_items(item_ids=["list-1", "list-2"])
        result = self._mgr.list_archived()
        self.assertEqual(len(result), 2)

    def test_list_archived_limit(self) -> None:
        """Параметр limit ограничивает количество результатов."""
        for i in range(10):
            self._store.add_fake_item(f"lim-{i}", f"Запись {i}")
            self._mgr.archive_items(item_ids=[f"lim-{i}"])
        result = self._mgr.list_archived(limit=3)
        self.assertEqual(len(result), 3)

    def test_list_archived_ids(self) -> None:
        """ID архивированных записей совпадают с переданными."""
        self._store.add_fake_item("id-check", "Проверка ID")
        self._mgr.archive_items(item_ids=["id-check"])
        result = self._mgr.list_archived()
        ids = [item["id"] for item in result]
        self.assertIn("id-check", ids)


class ArchiveManagerStatsTestCase(unittest.TestCase):
    """Тесты get_archive_stats."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def test_get_archive_stats_empty(self) -> None:
        """Статистика пустого архива: total_archived=0."""
        stats = self._mgr.get_archive_stats()
        self.assertEqual(stats["total_archived"], 0)
        self.assertIsNone(stats["oldest_ts"])
        self.assertIsNone(stats["newest_ts"])

    def test_get_archive_stats_keys(self) -> None:
        """Статистика содержит все ожидаемые ключи."""
        stats = self._mgr.get_archive_stats()
        for key in ("total_archived", "size_mb", "oldest_ts", "newest_ts", "archive_path"):
            self.assertIn(key, stats)

    def test_get_archive_stats_total(self) -> None:
        """total_archived соответствует числу архивированных записей."""
        self._store.add_fake_item("s1", "Стат 1")
        self._store.add_fake_item("s2", "Стат 2")
        self._mgr.archive_items(item_ids=["s1", "s2"])
        stats = self._mgr.get_archive_stats()
        self.assertEqual(stats["total_archived"], 2)

    def test_get_archive_stats_size_mb_non_negative(self) -> None:
        """Размер архива в МБ — неотрицательное число."""
        self._store.add_fake_item("sz1", "Размер")
        self._mgr.archive_items(item_ids=["sz1"])
        stats = self._mgr.get_archive_stats()
        self.assertGreaterEqual(stats["size_mb"], 0.0)

    def test_get_archive_stats_archive_path(self) -> None:
        """archive_path указывает на ожидаемый файл."""
        stats = self._mgr.get_archive_stats()
        expected_path = str(Path(self._tmpdir) / "archive" / "archive.ndjson")
        self.assertEqual(stats["archive_path"], expected_path)


class ArchiveManagerUnarchiveTestCase(unittest.TestCase):
    """Тесты unarchive_items."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def test_unarchive_items_restores_count(self) -> None:
        """Восстановление: unarchived_count корректен."""
        self._store.add_fake_item("u1", "Восстановить меня")
        self._mgr.archive_items(item_ids=["u1"])
        result = self._mgr.unarchive_items(item_ids=["u1"])
        self.assertEqual(result["unarchived_count"], 1)

    def test_unarchive_items_removed_from_archive(self) -> None:
        """После восстановления запись удалена из архива."""
        self._store.add_fake_item("u2", "Из архива")
        self._mgr.archive_items(item_ids=["u2"])
        self._mgr.unarchive_items(item_ids=["u2"])
        archived = self._mgr.list_archived()
        ids = [item["id"] for item in archived]
        self.assertNotIn("u2", ids)

    def test_unarchive_nonexistent_returns_not_found(self) -> None:
        """Восстановление несуществующих ID: они попадают в not_found."""
        result = self._mgr.unarchive_items(item_ids=["ghost-id"])
        self.assertIn("ghost-id", result["not_found"])

    def test_unarchive_empty_list(self) -> None:
        """Пустой список — 0 восстановлено."""
        result = self._mgr.unarchive_items(item_ids=[])
        self.assertEqual(result["unarchived_count"], 0)

    def test_unarchive_partial(self) -> None:
        """Частичное восстановление: только существующие ID восстанавливаются."""
        self._store.add_fake_item("p1", "Частично 1")
        self._mgr.archive_items(item_ids=["p1"])
        result = self._mgr.unarchive_items(item_ids=["p1", "missing-id"])
        self.assertEqual(result["unarchived_count"], 1)
        self.assertIn("missing-id", result["not_found"])


class ArchiveManagerIPCTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков ArchiveManager."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def test_handle_archive_items_returns_dict(self) -> None:
        """IPC archive_items возвращает словарь с archived_count."""
        self._store.add_fake_item("ipc-1", "IPC запись")
        result = self._mgr.handle_archive_items({"item_ids": ["ipc-1"]})
        self.assertIn("archived_count", result)
        self.assertIn("archive_path", result)
        self.assertIn("size_mb", result)
        self.assertEqual(result["archived_count"], 1)

    def test_handle_archive_items_invalid_params(self) -> None:
        """IPC archive_items с некорректным item_ids бросает ValueError."""
        with self.assertRaises((ValueError, TypeError)):
            self._mgr.handle_archive_items({"item_ids": "not-a-list"})

    def test_handle_list_archived_returns_items_and_total(self) -> None:
        """IPC list_archived возвращает items и total."""
        self._store.add_fake_item("ipc-list", "Список IPC")
        self._mgr.archive_items(item_ids=["ipc-list"])
        result = self._mgr.handle_list_archived({"limit": 10})
        self.assertIn("items", result)
        self.assertIn("total", result)
        self.assertEqual(result["total"], 1)

    def test_handle_get_archive_stats_returns_dict(self) -> None:
        """IPC get_archive_stats возвращает корректный словарь."""
        result = self._mgr.handle_get_archive_stats({})
        self.assertIn("total_archived", result)
        self.assertIsInstance(result["total_archived"], int)

    def test_handle_unarchive_items_returns_dict(self) -> None:
        """IPC unarchive_items возвращает словарь с unarchived_count."""
        self._store.add_fake_item("ipc-un", "Разархивировать")
        self._mgr.archive_items(item_ids=["ipc-un"])
        result = self._mgr.handle_unarchive_items({"item_ids": ["ipc-un"]})
        self.assertIn("unarchived_count", result)
        self.assertIn("not_found", result)


class ArchiveManagerPersistenceTestCase(unittest.TestCase):
    """Тесты персистентности архива через перезапуск менеджера."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_archive_persists_across_manager_restarts(self) -> None:
        """Архивированные данные сохраняются при создании нового ArchiveManager."""
        store = FakeStore(data_dir=self._tmpdir)
        mgr1 = ArchiveManager(store=store)
        store.add_fake_item("persist-1", "Персистентная запись")
        mgr1.archive_items(item_ids=["persist-1"])

        # Создаём новый менеджер для того же data_dir
        mgr2 = ArchiveManager(store=store)
        items = mgr2.list_archived()
        ids = [item["id"] for item in items]
        self.assertIn("persist-1", ids)

    def test_stats_after_archiving_multiple_items(self) -> None:
        """Статистика корректна после нескольких операций архивирования."""
        store = FakeStore(data_dir=self._tmpdir)
        mgr = ArchiveManager(store=store)
        for i in range(5):
            store.add_fake_item(f"multi-{i}", f"Много записей {i}")
            mgr.archive_items(item_ids=[f"multi-{i}"])
        stats = mgr.get_archive_stats()
        self.assertEqual(stats["total_archived"], 5)
        self.assertIsNotNone(stats["oldest_ts"])
        self.assertIsNotNone(stats["newest_ts"])

    def test_archive_result_path_matches_expected(self) -> None:
        """archive_path в ArchiveResult указывает на корректное место."""
        store = FakeStore(data_dir=self._tmpdir)
        mgr = ArchiveManager(store=store)
        store.add_fake_item("path-check", "Проверка пути")
        result = mgr.archive_items(item_ids=["path-check"])
        expected = str(Path(self._tmpdir) / "archive" / "archive.ndjson")
        self.assertEqual(result.archive_path, expected)


if __name__ == "__main__":
    unittest.main()
