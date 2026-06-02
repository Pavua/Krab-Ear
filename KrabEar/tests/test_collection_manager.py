"""Unit-тесты для CollectionManager."""

from __future__ import annotations
from backend.collection_manager import CollectionManager

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeHistoryItem:
    def __init__(self, item_id: str, text: str) -> None:
        self.id = item_id
        self.text = text

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


class FakeStore:
    """Минимальный фейк StateStore для тестов CollectionManager."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_fake_item(self, item_id: str, text: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


class CollectionManagerCRUDTestCase(unittest.TestCase):
    """Тесты создания, удаления и списка коллекций."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_create_collection_returns_dict(self) -> None:
        result = self._mgr.create_collection("Работа", "Рабочие записи")
        self.assertEqual(result["name"], "Работа")
        self.assertEqual(result["description"], "Рабочие записи")
        self.assertEqual(result["item_count"], 0)
        self.assertIn("created_at", result)

    def test_create_collection_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.create_collection("   ")

    def test_create_duplicate_collection_raises(self) -> None:
        self._mgr.create_collection("Дубль")
        with self.assertRaises(ValueError):
            self._mgr.create_collection("Дубль")

    def test_list_collections_empty(self) -> None:
        result = self._mgr.list_collections()
        self.assertEqual(result, [])

    def test_list_collections_after_create(self) -> None:
        self._mgr.create_collection("A")
        self._mgr.create_collection("B")
        result = self._mgr.list_collections()
        names = {c["name"] for c in result}
        self.assertIn("A", names)
        self.assertIn("B", names)

    def test_delete_existing_collection(self) -> None:
        self._mgr.create_collection("УдалитьМеня")
        deleted = self._mgr.delete_collection("УдалитьМеня")
        self.assertTrue(deleted)
        names = [c["name"] for c in self._mgr.list_collections()]
        self.assertNotIn("УдалитьМеня", names)

    def test_delete_nonexistent_collection_returns_false(self) -> None:
        result = self._mgr.delete_collection("НеСуществует")
        self.assertFalse(result)

    def test_collections_persisted_to_file(self) -> None:
        self._mgr.create_collection("Персист")
        path = Path(self._tmpdir) / "collections.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("Персист", data["collections"])

    def test_collections_loaded_from_existing_file(self) -> None:
        """Новый менеджер должен подхватить уже созданные коллекции."""
        self._mgr.create_collection("Сохранённая")
        mgr2 = CollectionManager(store=self._store)
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertIn("Сохранённая", names)


class CollectionManagerItemsTestCase(unittest.TestCase):
    """Тесты добавления/удаления элементов и получения содержимого коллекции."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)
        self._mgr.create_collection("Тест")

    def test_add_to_collection_increments_count(self) -> None:
        self._store.add_fake_item("id1", "текст 1")
        result = self._mgr.add_to_collection("Тест", "id1")
        self.assertEqual(result["item_count"], 1)

    def test_add_same_item_twice_is_idempotent(self) -> None:
        self._store.add_fake_item("id1", "текст 1")
        self._mgr.add_to_collection("Тест", "id1")
        result = self._mgr.add_to_collection("Тест", "id1")
        self.assertEqual(result["item_count"], 1)

    def test_add_to_nonexistent_collection_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.add_to_collection("НеСуществует", "id1")

    def test_add_empty_item_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("Тест", "  ")

    def test_remove_from_collection(self) -> None:
        self._store.add_fake_item("id1", "текст 1")
        self._mgr.add_to_collection("Тест", "id1")
        result = self._mgr.remove_from_collection("Тест", "id1")
        self.assertEqual(result["item_count"], 0)

    def test_remove_nonexistent_item_is_noop(self) -> None:
        result = self._mgr.remove_from_collection("Тест", "несуществующий")
        self.assertEqual(result["item_count"], 0)

    def test_remove_from_nonexistent_collection_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.remove_from_collection("НеТа", "id1")

    def test_get_collection_items_returns_history(self) -> None:
        self._store.add_fake_item("id1", "привет мир")
        self._mgr.add_to_collection("Тест", "id1")
        items = self._mgr.get_collection_items("Тест")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "id1")
        self.assertEqual(items[0]["text"], "привет мир")

    def test_get_collection_items_skips_deleted_items(self) -> None:
        """Записи, удалённые из истории, не должны попадать в результат."""
        self._mgr.add_to_collection("Тест", "не-существует")
        items = self._mgr.get_collection_items("Тест")
        self.assertEqual(items, [])

    def test_get_collection_items_unknown_collection_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.get_collection_items("НеСуществует")

    def test_multiple_items_order_preserved(self) -> None:
        for i in range(3):
            self._store.add_fake_item(f"id{i}", f"текст {i}")
            self._mgr.add_to_collection("Тест", f"id{i}")
        items = self._mgr.get_collection_items("Тест")
        self.assertEqual([it["id"] for it in items], ["id0", "id1", "id2"])


class CollectionManagerBulkTestCase(unittest.TestCase):
    """Тесты массового добавления элементов в коллекцию."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)
        self._mgr.create_collection("Bulk")

    def test_bulk_add_15_items(self) -> None:
        """Добавление 15 элементов: item_count должен быть ровно 15."""
        for i in range(15):
            self._store.add_fake_item(f"bulk_{i}", f"текст {i}")
            self._mgr.add_to_collection("Bulk", f"bulk_{i}")
        cols = self._mgr.list_collections()
        col = next(c for c in cols if c["name"] == "Bulk")
        self.assertEqual(col["item_count"], 15)

    def test_bulk_add_idempotent_deduplication(self) -> None:
        """Повторное добавление 10 элементов не создаёт дублей."""
        for i in range(10):
            self._store.add_fake_item(f"dup_{i}", f"текст {i}")
        for _ in range(3):
            for i in range(10):
                self._mgr.add_to_collection("Bulk", f"dup_{i}")
        cols = self._mgr.list_collections()
        col = next(c for c in cols if c["name"] == "Bulk")
        self.assertEqual(col["item_count"], 10)

    def test_bulk_add_and_partial_remove(self) -> None:
        """Добавить 12 элементов, удалить половину — остаток 6."""
        for i in range(12):
            self._store.add_fake_item(f"p_{i}", f"текст {i}")
            self._mgr.add_to_collection("Bulk", f"p_{i}")
        for i in range(6):
            self._mgr.remove_from_collection("Bulk", f"p_{i}")
        cols = self._mgr.list_collections()
        col = next(c for c in cols if c["name"] == "Bulk")
        self.assertEqual(col["item_count"], 6)

    def test_bulk_items_persisted_to_file(self) -> None:
        """После массового добавления данные корректно сохраняются в JSON."""
        for i in range(10):
            self._store.add_fake_item(f"pf_{i}", f"текст {i}")
            self._mgr.add_to_collection("Bulk", f"pf_{i}")
        data = json.loads(
            (Path(self._tmpdir) / "collections.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(data["collections"]["Bulk"]["item_ids"]), 10)

    def test_bulk_get_collection_items_order(self) -> None:
        """get_collection_items сохраняет порядок вставки при 10+ элементах."""
        ids = [f"ord_{i}" for i in range(10)]
        for item_id in ids:
            self._store.add_fake_item(item_id, f"текст {item_id}")
            self._mgr.add_to_collection("Bulk", item_id)
        items = self._mgr.get_collection_items("Bulk")
        self.assertEqual([it["id"] for it in items], ids)


class CollectionManagerIPCHandlersTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_handle_create_collection(self) -> None:
        result = self._mgr.handle_create_collection({"name": "ИПЦ", "description": "тест"})
        self.assertEqual(result["name"], "ИПЦ")

    def test_handle_create_collection_missing_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_create_collection({"description": "без имени"})

    def test_handle_delete_collection(self) -> None:
        self._mgr.create_collection("Удалить")
        result = self._mgr.handle_delete_collection({"name": "Удалить"})
        self.assertTrue(result["deleted"])

    def test_handle_delete_collection_missing_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_delete_collection({})

    def test_handle_list_collections(self) -> None:
        self._mgr.create_collection("Список")
        result = self._mgr.handle_list_collections({})
        self.assertIn("collections", result)
        names = [c["name"] for c in result["collections"]]
        self.assertIn("Список", names)

    def test_handle_add_to_collection(self) -> None:
        self._mgr.create_collection("ДобавитьСюда")
        self._store.add_fake_item("x1", "текст")
        result = self._mgr.handle_add_to_collection(
            {"collection_name": "ДобавитьСюда", "item_id": "x1"}
        )
        self.assertEqual(result["item_count"], 1)

    def test_handle_add_to_collection_missing_params_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_add_to_collection({"collection_name": "X"})

    def test_handle_remove_from_collection(self) -> None:
        self._mgr.create_collection("УдалитьИз")
        self._store.add_fake_item("x2", "текст")
        self._mgr.add_to_collection("УдалитьИз", "x2")
        result = self._mgr.handle_remove_from_collection(
            {"collection_name": "УдалитьИз", "item_id": "x2"}
        )
        self.assertEqual(result["item_count"], 0)

    def test_handle_get_collection_items(self) -> None:
        self._mgr.create_collection("ПолучитьИз")
        self._store.add_fake_item("y1", "содержимое")
        self._mgr.add_to_collection("ПолучитьИз", "y1")
        result = self._mgr.handle_get_collection_items({"collection_name": "ПолучитьИз"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["id"], "y1")

    def test_handle_get_collection_items_missing_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_get_collection_items({})

    def test_handle_add_to_nonexistent_collection_raises_runtime(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_add_to_collection(
                {"collection_name": "НеСуществует", "item_id": "id1"}
            )

    def test_item_count_in_list_reflects_members(self) -> None:
        self._mgr.create_collection("Счётчик")
        for i in range(5):
            self._store.add_fake_item(f"cnt{i}", f"текст {i}")
            self._mgr.add_to_collection("Счётчик", f"cnt{i}")
        cols = self._mgr.list_collections()
        col = next(c for c in cols if c["name"] == "Счётчик")
        self.assertEqual(col["item_count"], 5)


class CollectionManagerAtomicSaveTestCase(unittest.TestCase):
    """Тесты атомарной записи _save() — W964 / W954 F1 MED.

    Проверяем, что tmp-файл убирается после успешной записи и что повреждённый
    основной файл не обнуляет данные уже загруженного менеджера.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_atomic_save_no_partial_file(self) -> None:
        """После успешного _save() временный .tmp файл не остаётся на диске."""
        self._mgr.create_collection("АтомарнаяЗапись")
        tmp_path = Path(self._tmpdir) / "collections.json.tmp"
        # Временный файл должен исчезнуть после атомарного rename.
        self.assertFalse(
            tmp_path.exists(),
            "Временный .tmp файл не должен оставаться после успешной записи",
        )
        # Финальный файл должен быть валидным JSON.
        final_path = Path(self._tmpdir) / "collections.json"
        self.assertTrue(final_path.exists())
        data = json.loads(final_path.read_text(encoding="utf-8"))
        self.assertIn("АтомарнаяЗапись", data["collections"])

    def test_corrupt_file_does_not_affect_loaded_manager(self) -> None:
        """Повреждение файла после загрузки не уничтожает данные в памяти."""
        self._mgr.create_collection("ВПамяти")
        # Портим файл напрямую — эмулируем обрыв другим процессом.
        corrupt_path = Path(self._tmpdir) / "collections.json"
        corrupt_path.write_text("{INVALID JSON", encoding="utf-8")
        # Уже загруженный менеджер всё ещё знает о коллекции.
        names = [c["name"] for c in self._mgr.list_collections()]
        self.assertIn(
            "ВПамяти",
            names,
            "In-memory state должен оставаться нетронутым при повреждении файла на диске",
        )

    def test_load_logs_warning_on_corrupt_file(self) -> None:
        """_load() должен логировать warning с exc_info при повреждённом файле."""
        import logging

        corrupt_path = Path(self._tmpdir) / "collections.json"
        corrupt_path.write_text("{BAD", encoding="utf-8")

        with self.assertLogs("KrabEar.Backend.CollectionManager", level=logging.WARNING) as cm:
            # Создаём новый менеджер — он вызовет _load() на повреждённый файл.
            CollectionManager(store=self._store)

        # Хотя бы одно сообщение должно упоминать проблему загрузки.
        self.assertTrue(
            any("загрузить" in msg or "Не удалось" in msg for msg in cm.output),
            f"Ожидали warning о неудачной загрузке, получили: {cm.output}",
        )

    def test_save_followed_by_fresh_load_round_trips(self) -> None:
        """Данные, записанные атомарно, корректно считываются новым экземпляром."""
        self._mgr.create_collection("РаундТрип", "описание теста")
        self._store.add_fake_item("rt_id", "текст")
        self._mgr.add_to_collection("РаундТрип", "rt_id")

        # Создаём свежий менеджер — загружает файл с диска.
        mgr2 = CollectionManager(store=self._store)
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertIn("РаундТрип", names)
        col = next(c for c in mgr2.list_collections() if c["name"] == "РаундТрип")
        self.assertEqual(col["item_count"], 1)


class CollectionManagerSaveFailurePropagatesTestCase(unittest.TestCase):
    """FINDING 1 (MED W1769): сбой записи на диск НЕ должен возвращать ложный успех.

    Раньше _save() проглатывал любое исключение записи и возвращался нормально,
    из-за чего мутирующие методы возвращали {ok/deleted: true}, хотя ничего не
    записывалось. Теперь _save() пробрасывает исключение, а мутирующие методы
    дают ему распространиться → IPC-обработчик вернёт error-конверт (ok:false).

    Паттерн fail-before/pass-after: патчим атомарную запись (os.replace) так,
    чтобы она бросала OSError, и проверяем, что метод теперь поднимает исключение,
    а НЕ возвращает успех.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_create_collection_raises_on_save_failure(self) -> None:
        from unittest import mock

        with mock.patch(
            "backend.collection_manager.os.replace",
            side_effect=OSError(28, "No space left on device"),
        ):
            with self.assertRaises(OSError):
                self._mgr.create_collection("СбойДиска")

    def test_delete_collection_raises_on_save_failure(self) -> None:
        from unittest import mock

        self._mgr.create_collection("ДляУдаления")
        with mock.patch(
            "backend.collection_manager.os.replace",
            side_effect=OSError(13, "Permission denied"),
        ):
            with self.assertRaises(OSError):
                self._mgr.delete_collection("ДляУдаления")

    def test_add_to_collection_raises_on_save_failure(self) -> None:
        from unittest import mock

        self._mgr.create_collection("ДляДобавления")
        self._store.add_fake_item("item-1", "текст")
        with mock.patch(
            "backend.collection_manager.os.replace",
            side_effect=OSError(30, "Read-only file system"),
        ):
            with self.assertRaises(OSError):
                self._mgr.add_to_collection("ДляДобавления", "item-1")

    def test_remove_from_collection_raises_on_save_failure(self) -> None:
        from unittest import mock

        self._mgr.create_collection("ДляСнятия")
        self._store.add_fake_item("item-2", "текст")
        self._mgr.add_to_collection("ДляСнятия", "item-2")
        with mock.patch(
            "backend.collection_manager.os.replace",
            side_effect=OSError(28, "No space left on device"),
        ):
            with self.assertRaises(OSError):
                self._mgr.remove_from_collection("ДляСнятия", "item-2")

    def test_rename_collection_raises_on_save_failure(self) -> None:
        from unittest import mock

        self._mgr.create_collection("СтароеИмя")
        with mock.patch(
            "backend.collection_manager.os.replace",
            side_effect=OSError(13, "Permission denied"),
        ):
            with self.assertRaises(OSError):
                self._mgr.rename_collection("СтароеИмя", "НовоеИмя")

    def test_save_failure_logs_without_pii(self) -> None:
        """_save() при сбое логирует тип ошибки, но НЕ имя/описание коллекции."""
        import logging
        from unittest import mock

        with mock.patch(
            "backend.collection_manager.os.replace",
            side_effect=OSError(28, "No space left on device"),
        ):
            with self.assertLogs(
                "KrabEar.Backend.CollectionManager", level=logging.ERROR
            ) as cm:
                with self.assertRaises(OSError):
                    self._mgr.create_collection("СекретноеИмя", "секретное описание")

        joined = "\n".join(cm.output)
        # Имя/описание (free-text PII) не должны утечь в лог.
        self.assertNotIn("СекретноеИмя", joined)
        self.assertNotIn("секретное описание", joined)


class CollectionManagerPurgeAllTestCase(unittest.TestCase):
    """FINDING 2 (MED W1769): purge_all() стирает collections.json + .tmp и память."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_purge_all_removes_file_and_clears_memory(self) -> None:
        self._mgr.create_collection("Работа", "конфиденциальное описание")
        self._store.add_fake_item("h1", "текст")
        self._mgr.add_to_collection("Работа", "h1")

        col_path = Path(self._tmpdir) / "collections.json"
        self.assertTrue(col_path.exists(), "Файл должен существовать до purge_all")

        self._mgr.purge_all()

        # Файл удалён с диска.
        self.assertFalse(col_path.exists(), "collections.json должен быть удалён")
        # In-memory состояние пустое.
        self.assertEqual(self._mgr.list_collections(), [])
        # Свежий менеджер не видит коллекций (PII не переживает purge).
        mgr2 = CollectionManager(store=self._store)
        self.assertEqual(mgr2.list_collections(), [])

    def test_purge_all_removes_tmp_sibling(self) -> None:
        """purge_all() удаляет осиротевший .tmp-файл от прерванной записи."""
        self._mgr.create_collection("ЕстьЧтоУдалять")
        tmp_path = (Path(self._tmpdir) / "collections.json").with_suffix(
            ".json.tmp"
        )
        # Эмулируем .tmp, оставшийся от прерванной атомарной записи.
        tmp_path.write_text("{}", encoding="utf-8")
        self.assertTrue(tmp_path.exists())

        self._mgr.purge_all()

        self.assertFalse(tmp_path.exists(), ".tmp-сосед должен быть удалён")

    def test_purge_all_idempotent_second_call_noop(self) -> None:
        """Повторный purge_all() при отсутствии файлов не бросает исключений."""
        self._mgr.create_collection("Разово")
        self._mgr.purge_all()
        # Второй вызов на уже пустом состоянии/отсутствующем файле — no-op.
        self._mgr.purge_all()
        self.assertEqual(self._mgr.list_collections(), [])

    def test_purge_all_on_fresh_manager_no_file(self) -> None:
        """purge_all() на менеджере без единой записи (файла нет) — no-op."""
        col_path = Path(self._tmpdir) / "collections.json"
        self.assertFalse(col_path.exists())
        self._mgr.purge_all()  # не должно бросать
        self.assertEqual(self._mgr.list_collections(), [])


if __name__ == "__main__":
    unittest.main()
