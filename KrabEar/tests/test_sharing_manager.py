"""Unit-тесты для SharingManager."""

from __future__ import annotations
from backend.sharing_manager import SharingManager, SharePackage, SUPPORTED_FORMATS

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Вспомогательные фейки
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    """Минимальная заглушка HistoryItem."""

    def __init__(
        self,
        item_id: str,
        text: str,
        ts: str = "2024-01-01T10:00:00+00:00",
        translated_text: str = "",
        source_lang: str = "ru",
        target_lang: str = "es",
    ) -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.translated_text = translated_text
        self.source_lang = source_lang
        self.target_lang = target_lang

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "ts": self.ts,
            "translated_text": self.translated_text,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
        }


class FakeStore:
    """Минимальный фейк StateStore для тестов SharingManager."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_fake_item(self, item_id: str, text: str, **kwargs: Any) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text, **kwargs)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)


# ---------------------------------------------------------------------------
# Тесты generate_share_id
# ---------------------------------------------------------------------------

class GenerateShareIdTestCase(unittest.TestCase):
    """Тесты генерации уникального ID."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)

    def test_share_id_length_is_8(self) -> None:
        sid = self._mgr.generate_share_id()
        self.assertEqual(len(sid), 8)

    def test_share_id_is_alphanumeric(self) -> None:
        sid = self._mgr.generate_share_id()
        self.assertTrue(sid.isalnum(), f"ID содержит не алфанумерные символы: {sid!r}")

    def test_share_ids_are_unique(self) -> None:
        ids = {self._mgr.generate_share_id() for _ in range(50)}
        # С 62^8 возможными комбинациями коллизия за 50 попыток крайне маловероятна
        self.assertGreater(len(ids), 40)


# ---------------------------------------------------------------------------
# Тесты prepare_share
# ---------------------------------------------------------------------------

class PrepareShareTestCase(unittest.TestCase):
    """Тесты метода prepare_share."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)
        self._store.add_fake_item("id1", "Привет мир")
        self._store.add_fake_item("id2", "Второй элемент", translated_text="Second item")

    def test_returns_share_package_instance(self) -> None:
        pkg = self._mgr.prepare_share(["id1"])
        self.assertIsInstance(pkg, SharePackage)

    def test_share_package_has_required_fields(self) -> None:
        pkg = self._mgr.prepare_share(["id1"])
        self.assertTrue(pkg.share_id)
        self.assertTrue(pkg.content)
        self.assertTrue(pkg.filename)
        self.assertGreater(pkg.size_bytes, 0)
        self.assertTrue(pkg.created_at)

    def test_markdown_format_default(self) -> None:
        pkg = self._mgr.prepare_share(["id1"])
        self.assertIn(".md", pkg.filename)
        self.assertIn("Привет мир", pkg.content)

    def test_text_format(self) -> None:
        pkg = self._mgr.prepare_share(["id1"], format="text")
        self.assertIn(".txt", pkg.filename)
        self.assertIn("Привет мир", pkg.content)

    def test_json_format(self) -> None:
        pkg = self._mgr.prepare_share(["id1"], format="json")
        self.assertIn(".json", pkg.filename)
        data = json.loads(pkg.content)
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["id"], "id1")

    def test_include_translation_true(self) -> None:
        pkg = self._mgr.prepare_share(["id2"], format="markdown", include_translation=True)
        self.assertIn("Second item", pkg.content)

    def test_include_translation_false_excludes_translation(self) -> None:
        pkg = self._mgr.prepare_share(["id2"], format="markdown", include_translation=False)
        self.assertNotIn("Second item", pkg.content)

    def test_json_without_translation_excludes_fields(self) -> None:
        pkg = self._mgr.prepare_share(["id2"], format="json", include_translation=False)
        data = json.loads(pkg.content)
        self.assertNotIn("translated_text", data[0])

    def test_multiple_items(self) -> None:
        pkg = self._mgr.prepare_share(["id1", "id2"], format="text")
        self.assertIn("Привет мир", pkg.content)
        self.assertIn("Второй элемент", pkg.content)

    def test_empty_item_ids_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.prepare_share([])

    def test_unsupported_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.prepare_share(["id1"], format="pdf")

    def test_unknown_item_ids_skipped(self) -> None:
        # Несуществующие ID не должны приводить к исключению — просто пропускаются
        pkg = self._mgr.prepare_share(["id1", "не-существует"])
        self.assertIn("Привет мир", pkg.content)

    def test_size_bytes_matches_content(self) -> None:
        pkg = self._mgr.prepare_share(["id1"])
        self.assertEqual(pkg.size_bytes, len(pkg.content.encode("utf-8")))

    def test_file_saved_on_disk(self) -> None:
        pkg = self._mgr.prepare_share(["id1"])
        file_path = Path(self._tmpdir) / "shares" / pkg.filename
        self.assertTrue(file_path.exists())

    def test_file_content_matches_package_content(self) -> None:
        pkg = self._mgr.prepare_share(["id1"])
        file_path = Path(self._tmpdir) / "shares" / pkg.filename
        self.assertEqual(file_path.read_text(encoding="utf-8"), pkg.content)


# ---------------------------------------------------------------------------
# Тесты list_shared / get_shared
# ---------------------------------------------------------------------------

class ListAndGetSharedTestCase(unittest.TestCase):
    """Тесты list_shared и get_shared."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)
        self._store.add_fake_item("a1", "текст А")
        self._store.add_fake_item("b1", "текст Б")

    def test_list_shared_empty_initially(self) -> None:
        result = self._mgr.list_shared()
        self.assertEqual(result, [])

    def test_list_shared_after_prepare(self) -> None:
        self._mgr.prepare_share(["a1"])
        result = self._mgr.list_shared()
        self.assertEqual(len(result), 1)

    def test_list_shared_multiple_packages(self) -> None:
        self._mgr.prepare_share(["a1"])
        self._mgr.prepare_share(["b1"])
        result = self._mgr.list_shared()
        self.assertEqual(len(result), 2)

    def test_list_shared_no_content_field(self) -> None:
        """list_shared не должен возвращать тяжёлый content."""
        self._mgr.prepare_share(["a1"])
        result = self._mgr.list_shared()
        self.assertNotIn("content", result[0])

    def test_list_shared_has_metadata_fields(self) -> None:
        self._mgr.prepare_share(["a1"])
        entry = self._mgr.list_shared()[0]
        self.assertIn("share_id", entry)
        self.assertIn("filename", entry)
        self.assertIn("size_bytes", entry)
        self.assertIn("created_at", entry)

    def test_get_shared_returns_package(self) -> None:
        pkg = self._mgr.prepare_share(["a1"])
        found = self._mgr.get_shared(pkg.share_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.share_id, pkg.share_id)
        self.assertEqual(found.content, pkg.content)

    def test_get_shared_unknown_returns_none(self) -> None:
        result = self._mgr.get_shared("nonexistent")
        self.assertIsNone(result)

    def test_index_persisted_across_instances(self) -> None:
        """Созданные пакеты должны быть доступны в новом экземпляре SharingManager."""
        pkg = self._mgr.prepare_share(["a1"])
        mgr2 = SharingManager(store=self._store)
        found = mgr2.get_shared(pkg.share_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.share_id, pkg.share_id)


# ---------------------------------------------------------------------------
# Тесты IPC-обработчиков
# ---------------------------------------------------------------------------

class IPCHandlersTestCase(unittest.TestCase):
    """Тесты IPC-обёрток handle_prepare_share / handle_list_shared / handle_get_shared."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)
        self._store.add_fake_item("x1", "текст X")

    def test_handle_prepare_share_returns_dict(self) -> None:
        result = self._mgr.handle_prepare_share({"item_ids": ["x1"]})
        self.assertIsInstance(result, dict)
        self.assertIn("share_id", result)
        self.assertIn("content", result)

    def test_handle_prepare_share_missing_item_ids_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_prepare_share({})

    def test_handle_prepare_share_empty_item_ids_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_prepare_share({"item_ids": []})

    def test_handle_prepare_share_with_format(self) -> None:
        result = self._mgr.handle_prepare_share({"item_ids": ["x1"], "format": "json"})
        self.assertIn(".json", result["filename"])

    def test_handle_list_shared_returns_dict_with_shares(self) -> None:
        self._mgr.prepare_share(["x1"])
        result = self._mgr.handle_list_shared({})
        self.assertIn("shares", result)
        self.assertEqual(len(result["shares"]), 1)

    def test_handle_get_shared_returns_package_dict(self) -> None:
        pkg = self._mgr.prepare_share(["x1"])
        result = self._mgr.handle_get_shared({"share_id": pkg.share_id})
        self.assertEqual(result["share_id"], pkg.share_id)

    def test_handle_get_shared_missing_share_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_get_shared({})

    def test_handle_get_shared_unknown_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_get_shared({"share_id": "nonexistent"})

    def test_handle_prepare_share_include_translation_false(self) -> None:
        self._store.add_fake_item("tr1", "текст", translated_text="translation")
        result = self._mgr.handle_prepare_share(
            {"item_ids": ["tr1"], "format": "markdown", "include_translation": False}
        )
        self.assertNotIn("translation", result["content"])


# ---------------------------------------------------------------------------
# Тесты форматов рендеринга
# ---------------------------------------------------------------------------

class RenderFormatsTestCase(unittest.TestCase):
    """Тесты корректности рендеринга в каждом формате."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)
        self._store.add_fake_item(
            "r1",
            "Тест рендеринга",
            ts="2024-06-15T12:00:00+00:00",
            translated_text="Render test",
            source_lang="ru",
            target_lang="en",
        )

    def test_markdown_contains_header(self) -> None:
        pkg = self._mgr.prepare_share(["r1"], format="markdown")
        self.assertIn("#", pkg.content)

    def test_markdown_contains_timestamp(self) -> None:
        pkg = self._mgr.prepare_share(["r1"], format="markdown")
        self.assertIn("2024-06-15", pkg.content)

    def test_text_contains_item_text(self) -> None:
        pkg = self._mgr.prepare_share(["r1"], format="text")
        self.assertIn("Тест рендеринга", pkg.content)

    def test_json_is_valid_json_list(self) -> None:
        pkg = self._mgr.prepare_share(["r1"], format="json")
        data = json.loads(pkg.content)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_json_item_has_id_and_text(self) -> None:
        pkg = self._mgr.prepare_share(["r1"], format="json")
        data = json.loads(pkg.content)
        self.assertEqual(data[0]["id"], "r1")
        self.assertEqual(data[0]["text"], "Тест рендеринга")

    def test_all_supported_formats_work(self) -> None:
        for fmt in SUPPORTED_FORMATS:
            with self.subTest(format=fmt):
                pkg = self._mgr.prepare_share(["r1"], format=fmt)
                self.assertGreater(len(pkg.content), 0)


# ---------------------------------------------------------------------------
# Тесты персистентности и очистки
# ---------------------------------------------------------------------------

class PersistenceAndCleanupTestCase(unittest.TestCase):
    """Тесты сохранения индекса и очистки старых пакетов."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)
        self._store.add_fake_item("p1", "текст для сохранения")

    def test_index_file_created_after_prepare_share(self) -> None:
        """После prepare_share должен создаться shares_index.json."""
        self._mgr.prepare_share(["p1"])
        index_path = Path(self._tmpdir) / "shares" / "shares_index.json"
        self.assertTrue(index_path.exists())

    def test_index_file_valid_json(self) -> None:
        """Сохранённый индекс должен быть валидным JSON."""
        self._mgr.prepare_share(["p1"])
        index_path = Path(self._tmpdir) / "shares" / "shares_index.json"
        content = index_path.read_text(encoding="utf-8")
        data = json.loads(content)
        self.assertIsInstance(data, dict)

    def test_shares_dir_created(self) -> None:
        """Должна быть создана директория shares."""
        self._mgr.prepare_share(["p1"])
        shares_dir = Path(self._tmpdir) / "shares"
        self.assertTrue(shares_dir.is_dir())

    def test_index_survives_manager_reload(self) -> None:
        """После перезагрузки SharingManager индекс должен быть восстановлен."""
        pkg1 = self._mgr.prepare_share(["p1"])

        # Создаём новый экземпляр, который загружает индекс с диска
        mgr2 = SharingManager(store=self._store)

        # Старый пакет должен быть виден
        found = mgr2.get_shared(pkg1.share_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.share_id, pkg1.share_id)

    def test_invalid_index_file_ignored_gracefully(self) -> None:
        """Если индекс повреждён, должен создаться пустой индекс без ошибки."""
        # Создаём повреждённый индекс
        index_path = Path(self._tmpdir) / "shares" / "shares_index.json"
        index_path.write_text("not valid json {", encoding="utf-8")

        # Новый экземпляр должен игнорировать повреждённый файл
        mgr2 = SharingManager(store=self._store)
        # list_shared должен вернуть пустой список (индекс переинициализирован)
        self.assertEqual(mgr2.list_shared(), [])


# ---------------------------------------------------------------------------
# Тесты потокобезопасности
# ---------------------------------------------------------------------------

class ThreadSafetyTestCase(unittest.TestCase):
    """Тесты потокобезопасности SharingManager."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)
        # Добавляем несколько элементов для параллельного использования
        for i in range(10):
            self._store.add_fake_item(f"item_{i}", f"текст {i}")

    def test_concurrent_prepare_share_calls(self) -> None:
        """Параллельные вызовы prepare_share должны работать без коллизий."""
        import threading

        results = []
        lock = threading.Lock()

        def prepare_and_store(item_id: str) -> None:
            pkg = self._mgr.prepare_share([item_id])
            with lock:
                results.append(pkg.share_id)

        threads = []
        for i in range(5):
            t = threading.Thread(target=prepare_and_store, args=(f"item_{i}",))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Все вызовы должны завершиться успешно
        self.assertEqual(len(results), 5)
        # Все ID должны быть уникальны
        self.assertEqual(len(set(results)), 5)

    def test_concurrent_list_and_prepare(self) -> None:
        """Одновременные list_shared и prepare_share должны работать корректно."""
        import threading

        list_results = []
        lock = threading.Lock()

        def prepare_share_thread() -> None:
            for i in range(3):
                self._mgr.prepare_share([f"item_{i}"])

        def list_share_thread() -> None:
            for _ in range(3):
                result = self._mgr.list_shared()
                with lock:
                    list_results.append(len(result))

        t1 = threading.Thread(target=prepare_share_thread)
        t2 = threading.Thread(target=list_share_thread)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # list_shared вызывался 3 раза и всегда возвращал корректные длины
        self.assertEqual(len(list_results), 3)
        # Все значения должны быть целые числа >= 0
        for length in list_results:
            self.assertIsInstance(length, int)
            self.assertGreaterEqual(length, 0)


# ---------------------------------------------------------------------------
# Тесты граничных случаев
# ---------------------------------------------------------------------------

class EdgeCasesTestCase(unittest.TestCase):
    """Тесты граничных случаев и исключительных ситуаций."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store)

    def test_very_large_content(self) -> None:
        """Тест с очень большим контентом (10MB)."""
        large_text = "x" * (10 * 1024 * 1024)  # 10MB
        self._store.add_fake_item("big", large_text)
        pkg = self._mgr.prepare_share(["big"])
        self.assertGreater(pkg.size_bytes, 10 * 1024 * 1024)

    def test_unicode_content(self) -> None:
        """Тест с разнообразным юникодом."""
        unicode_text = "Русский текст 中文 العربية 🚀 emoji"
        self._store.add_fake_item("unicode", unicode_text)
        pkg = self._mgr.prepare_share(["unicode"], format="json")
        data = json.loads(pkg.content)
        self.assertEqual(data[0]["text"], unicode_text)

    def test_format_case_insensitive(self) -> None:
        """Формат должен быть case-insensitive."""
        self._store.add_fake_item("case", "тест")
        pkg1 = self._mgr.prepare_share(["case"], format="MARKDOWN")
        pkg2 = self._mgr.prepare_share(["case"], format="markdown")
        # Оба должны создать .md файлы
        self.assertTrue(pkg1.filename.endswith(".md"))
        self.assertTrue(pkg2.filename.endswith(".md"))

    def test_whitespace_in_format_trimmed(self) -> None:
        """Пробелы в формате должны обрезаться."""
        self._store.add_fake_item("space", "тест")
        pkg = self._mgr.prepare_share(["space"], format="  text  ")
        self.assertIn(".txt", pkg.filename)

    def test_share_id_uniqueness_collision_resistance(self) -> None:
        """После 20 попыток generate_share_id должно гарантироваться создание уникального ID."""
        # Получаем ID, заполняем индекс до 20+ значений
        pkg_ids = []
        for i in range(25):
            self._store.add_fake_item(f"item_{i}", f"текст {i}")
            pkg = self._mgr.prepare_share([f"item_{i}"])
            pkg_ids.append(pkg.share_id)

        # Все ID должны быть уникальны
        self.assertEqual(len(pkg_ids), len(set(pkg_ids)))

    def test_special_characters_in_filenames(self) -> None:
        """Проверяем, что filename всегда содержит валидные символы."""
        self._store.add_fake_item("special", "текст")
        pkg = self._mgr.prepare_share(["special"])
        # Filename должен содержать только буквы, цифры, подчёркивание, точку
        self.assertRegex(pkg.filename, r'^[\w\-\.]+\.(?:md|txt|json)$')


if __name__ == "__main__":
    unittest.main()
