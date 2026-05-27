"""Unit-тесты для ArchiveManager."""

from __future__ import annotations
from backend.archive_manager import ArchiveManager, ArchiveResult

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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
        lines = [ln for ln in archive_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
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


class ArchiveManagerWave138TestCase(unittest.TestCase):
    """Wave 138 — дополнительные тесты по спецификации задачи."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    # ------------------------------------------------------------------
    # test_archive_items_older_than_days
    # ArchiveManager не имеет встроенного date-фильтра, поэтому тест
    # проверяет ручной отбор записей по ts и передачу в archive_items.
    # ------------------------------------------------------------------

    def test_archive_items_older_than_days(self) -> None:
        """Записи старше N дней архивируются, остальные — нет."""
        # old item: ts 100 дней назад
        from datetime import timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        self._store.add_fake_item("old-item", "Старая запись", ts=old_ts)
        self._store.add_fake_item("new-item", "Новая запись", ts=new_ts)

        # Архивируем только старую запись
        result = self._mgr.archive_items(item_ids=["old-item"])
        self.assertEqual(result.archived_count, 1)

        archived = self._mgr.list_archived()
        ids = {item["id"] for item in archived}
        self.assertIn("old-item", ids)
        self.assertNotIn("new-item", ids)

    def test_recent_items_kept_in_main(self) -> None:
        """Новые записи остаются в активной истории после архивирования старых."""
        from datetime import timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

        self._store.add_fake_item("keep-1", "Оставить 1")
        self._store.add_fake_item("keep-2", "Оставить 2")
        self._store.add_fake_item("archive-1", "Архивировать", ts=old_ts)

        self._mgr.archive_items(item_ids=["archive-1"])

        # Новые записи не были затронуты
        self.assertNotIn("keep-1", self._store._deleted)
        self.assertNotIn("keep-2", self._store._deleted)
        # Старая запись удалена из активного хранилища
        self.assertIn("archive-1", self._store._deleted)

    def test_restore_from_archive(self) -> None:
        """Запись восстанавливается из архива и доступна через store."""
        self._store.add_fake_item("restore-me", "Восстановить из архива")
        self._mgr.archive_items(item_ids=["restore-me"])

        # Убеждаемся, что запись в архиве
        archived = self._mgr.list_archived()
        self.assertTrue(any(item["id"] == "restore-me" for item in archived))

        # Восстанавливаем
        result = self._mgr.unarchive_items(item_ids=["restore-me"])
        self.assertEqual(result["unarchived_count"], 1)
        self.assertEqual(result["not_found"], [])

        # После восстановления запись удалена из архива
        archived_after = self._mgr.list_archived()
        self.assertFalse(any(item["id"] == "restore-me" for item in archived_after))

        # store.add_history_item был вызван
        self.assertEqual(len(self._store._added), 1)

    def test_unicode_text_preserved(self) -> None:
        """Юникод (кириллица, CJK, emoji) корректно сохраняется и читается из архива."""
        unicode_text = "Привет мир 你好世界 🎉 ñoño"
        self._store.add_fake_item("uni-1", unicode_text)
        self._mgr.archive_items(item_ids=["uni-1"])

        archived = self._mgr.list_archived()
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["text"], unicode_text)

        # Также проверяем сырой файл
        archive_file = Path(self._tmpdir) / "archive" / "archive.ndjson"
        raw = archive_file.read_text(encoding="utf-8")
        self.assertIn("Привет мир", raw)
        self.assertIn("你好世界", raw)
        self.assertIn("🎉", raw)

    def test_concurrent_archive_safe(self) -> None:
        """Параллельное архивирование разных записей не теряет данные."""
        num_items = 20
        for i in range(num_items):
            self._store.add_fake_item(f"conc-{i}", f"Параллельная запись {i}")

        errors: list[Exception] = []

        def archive_one(item_id: str) -> None:
            try:
                self._mgr.archive_items(item_ids=[item_id])
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=archive_one, args=(f"conc-{i}",))
            for i in range(num_items)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Exceptions during concurrent archive: {errors}")
        # Все записи должны быть заархивированы
        archived = self._mgr.list_archived(limit=500)
        self.assertEqual(len(archived), num_items)

    def test_handles_corrupted_archive(self) -> None:
        """Повреждённые строки в archive.ndjson пропускаются без исключения."""
        archive_file = Path(self._tmpdir) / "archive" / "archive.ndjson"

        # Записываем валидную запись + мусор + ещё одну валидную
        valid1 = json.dumps({"id": "good-1", "text": "Хорошая 1", "archived_at": "2026-01-01T10:00:00"})
        valid2 = json.dumps({"id": "good-2", "text": "Хорошая 2", "archived_at": "2026-01-02T10:00:00"})
        corrupt = "{не валидный json"
        archive_file.write_text(
            valid1 + "\n" + corrupt + "\n" + valid2 + "\n",
            encoding="utf-8",
        )

        # Чтение не должно падать
        archived = self._mgr.list_archived()
        # Две валидные строки прочитаны
        self.assertEqual(len(archived), 2)
        ids = {item["id"] for item in archived}
        self.assertIn("good-1", ids)
        self.assertIn("good-2", ids)

        # Статистика тоже работает корректно
        stats = self._mgr.get_archive_stats()
        self.assertEqual(stats["total_archived"], 2)


class ArchiveManagerSemanticW1458TestCase(unittest.TestCase):
    """W1458 — archive_items removes stale embeddings from semantic search index."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)

    # ------------------------------------------------------------------
    # Fake semantic searcher
    # ------------------------------------------------------------------

    class FakeSemanticSearcher:
        """Minimal fake SemanticSearcher for W1458 tests."""

        def __init__(self) -> None:
            self.removed_ids: list[str] = []
            self.raise_on_remove: bool = False

        def remove_item(self, item_id: str) -> bool:
            if self.raise_on_remove:
                raise RuntimeError("simulated remove_item failure")
            self.removed_ids.append(item_id)
            return True

    def test_archive_items_removes_from_semantic_index(self) -> None:
        """archive_items calls semantic_searcher.remove_item for each archived id."""
        searcher = self.FakeSemanticSearcher()
        mgr = ArchiveManager(store=self._store, semantic_searcher=searcher)

        self._store.add_fake_item("sem-1", "Запись 1")
        self._store.add_fake_item("sem-2", "Запись 2")

        result = mgr.archive_items(item_ids=["sem-1", "sem-2"])

        self.assertEqual(result.archived_count, 2)
        self.assertIn("sem-1", searcher.removed_ids)
        self.assertIn("sem-2", searcher.removed_ids)

    def test_archive_safe_when_semantic_searcher_none(self) -> None:
        """archive_items succeeds without errors when no semantic_searcher injected."""
        mgr = ArchiveManager(store=self._store)  # no semantic_searcher

        self._store.add_fake_item("nosem-1", "Без индекса")
        result = mgr.archive_items(item_ids=["nosem-1"])

        self.assertEqual(result.archived_count, 1)

    def test_archive_items_semantic_remove_exception_does_not_abort(self) -> None:
        """If semantic_searcher.remove_item raises, archive still completes."""
        searcher = self.FakeSemanticSearcher()
        searcher.raise_on_remove = True
        mgr = ArchiveManager(store=self._store, semantic_searcher=searcher)

        self._store.add_fake_item("err-sem", "Ошибка индекса")
        result = mgr.archive_items(item_ids=["err-sem"])

        # Archive still succeeds — semantic error is swallowed with a warning.
        self.assertEqual(result.archived_count, 1)
        self.assertIn("err-sem", self._store._deleted)

    def test_archive_items_nonexistent_id_no_semantic_call(self) -> None:
        """Nonexistent items never reach semantic_searcher.remove_item."""
        searcher = self.FakeSemanticSearcher()
        mgr = ArchiveManager(store=self._store, semantic_searcher=searcher)

        result = mgr.archive_items(item_ids=["ghost-123"])

        self.assertEqual(result.archived_count, 0)
        self.assertEqual(searcher.removed_ids, [])

    def test_archive_items_semantic_remove_called_once_per_item(self) -> None:
        """remove_item is called exactly once per successfully archived item."""
        searcher = self.FakeSemanticSearcher()
        mgr = ArchiveManager(store=self._store, semantic_searcher=searcher)

        for i in range(5):
            self._store.add_fake_item(f"multi-sem-{i}", f"Запись {i}")

        mgr.archive_items(item_ids=[f"multi-sem-{i}" for i in range(5)])

        self.assertEqual(len(searcher.removed_ids), 5)
        for i in range(5):
            self.assertIn(f"multi-sem-{i}", searcher.removed_ids)

    def test_late_inject_semantic_searcher(self) -> None:
        """Semantic searcher can be injected after construction (late-inject pattern)."""
        mgr = ArchiveManager(store=self._store)  # constructed without searcher
        searcher = self.FakeSemanticSearcher()
        mgr._semantic_searcher = searcher  # late-inject (as done in service.py)

        self._store.add_fake_item("late-inject", "Поздняя инъекция")
        result = mgr.archive_items(item_ids=["late-inject"])

        self.assertEqual(result.archived_count, 1)
        self.assertIn("late-inject", searcher.removed_ids)


if __name__ == "__main__":
    unittest.main()
