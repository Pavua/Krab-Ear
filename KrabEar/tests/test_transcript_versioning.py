"""Тесты для модуля версионирования транскрипций (TranscriptVersionManager).

Покрывает:
- Создание новой версии текста (save_version)
- Получение всех версий для записи (get_versions)
- Получение конкретной версии (get_version)
- Откат на предыдущую версию (revert_to_version)
- Различие между версиями (diff_versions)
- Персистентность (сохранение в NDJSON)
- Пустое состояние (no versions exist yet)
- Валидация параметров (item_id, text, source)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcript_versioning import TranscriptVersionManager


class TestTranscriptVersionManagerCreateVersion(unittest.TestCase):
    """Тесты создания новых версий."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_save_version_basic(self) -> None:
        """Сохраняет новую версию с корректными полями."""
        result = self.manager.save_version(
            item_id="item_001",
            text="Здравствуй, мир!",
            source="manual",
        )
        self.assertEqual(result["item_id"], "item_001")
        self.assertEqual(result["text"], "Здравствуй, мир!")
        self.assertEqual(result["source"], "manual")
        self.assertEqual(result["version_num"], 1)
        self.assertIn("created_at", result)

    def test_save_version_auto_increment(self) -> None:
        """Номера версий автоматически увеличиваются для одной записи."""
        self.manager.save_version("item_001", "Версия 1", "stt_raw")
        result2 = self.manager.save_version("item_001", "Версия 2", "manual")
        result3 = self.manager.save_version("item_001", "Версия 3", "llm_rewrite")

        self.assertEqual(result2["version_num"], 2)
        self.assertEqual(result3["version_num"], 3)

    def test_save_version_different_items_independent(self) -> None:
        """Разные item_id имеют независимые номера версий."""
        r1 = self.manager.save_version("item_a", "Text A1", "manual")
        r2 = self.manager.save_version("item_b", "Text B1", "manual")
        r3 = self.manager.save_version("item_a", "Text A2", "manual")

        self.assertEqual(r1["version_num"], 1)
        self.assertEqual(r2["version_num"], 1)
        self.assertEqual(r3["version_num"], 2)

    def test_save_version_default_source(self) -> None:
        """Источник по умолчанию — 'manual'."""
        result = self.manager.save_version("item_001", "Текст")
        self.assertEqual(result["source"], "manual")

    def test_save_version_all_sources(self) -> None:
        """Поддерживаются все валидные источники."""
        sources = ["stt_raw", "stt_cleaned", "llm_rewrite", "manual", "import"]
        for src in sources:
            result = self.manager.save_version(f"item_{src}", "Text", src)
            self.assertEqual(result["source"], src)

    def test_save_version_empty_item_id_raises(self) -> None:
        """Пустой item_id вызывает ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.save_version("", "Text")
        self.assertIn("item_id", str(ctx.exception))

    def test_save_version_non_string_text_raises(self) -> None:
        """Не-строка как text вызывает ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.save_version("item_001", 123)  # type: ignore
        self.assertIn("text", str(ctx.exception))

    def test_save_version_invalid_source_raises(self) -> None:
        """Невалидный source вызывает ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.save_version("item_001", "Text", "invalid_source")
        self.assertIn("Недопустимый source", str(ctx.exception))

    def test_save_version_preserves_timestamp(self) -> None:
        """Версия содержит ISO8601 timestamp."""
        result = self.manager.save_version("item_001", "Text")
        created_at = result["created_at"]
        # Проверяем, что это ISO8601 строка
        parsed = datetime.fromisoformat(created_at)
        self.assertIsInstance(parsed, datetime)


class TestTranscriptVersionManagerRetrieve(unittest.TestCase):
    """Тесты получения версий."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)
        # Создаём несколько версий для тестирования
        self.manager.save_version("item_001", "Text v1", "stt_raw")
        self.manager.save_version("item_001", "Text v2", "manual")
        self.manager.save_version("item_001", "Text v3", "llm_rewrite")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_versions_returns_all(self) -> None:
        """get_versions возвращает все версии для item_id."""
        versions = self.manager.get_versions("item_001")
        self.assertEqual(len(versions), 3)

    def test_get_versions_sorted_reverse(self) -> None:
        """get_versions возвращает версии в порядке убывания (новые первыми)."""
        versions = self.manager.get_versions("item_001")
        version_nums = [v["version_num"] for v in versions]
        self.assertEqual(version_nums, [3, 2, 1])

    def test_get_versions_empty_item(self) -> None:
        """get_versions для несуществующей записи возвращает пустой список."""
        versions = self.manager.get_versions("nonexistent_item")
        self.assertEqual(versions, [])

    def test_get_version_specific(self) -> None:
        """get_version возвращает конкретную версию."""
        result = self.manager.get_version("item_001", 2)
        self.assertEqual(result["version_num"], 2)
        self.assertEqual(result["text"], "Text v2")
        self.assertEqual(result["source"], "manual")

    def test_get_version_not_found_raises(self) -> None:
        """get_version для несуществующей версии вызывает KeyError."""
        with self.assertRaises(KeyError):
            self.manager.get_version("item_001", 99)

    def test_get_version_wrong_item_raises(self) -> None:
        """get_version для неправильного item_id вызывает KeyError."""
        with self.assertRaises(KeyError):
            self.manager.get_version("wrong_item", 1)


class TestTranscriptVersionManagerRevert(unittest.TestCase):
    """Тесты отката на предыдущую версию."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)
        self.manager.save_version("item_001", "Original text", "stt_raw")
        self.manager.save_version("item_001", "Modified text", "manual")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_revert_creates_new_version(self) -> None:
        """Откат создаёт новую версию с текстом из целевой версии."""
        reverted = self.manager.revert_to_version("item_001", 1)
        self.assertEqual(reverted["version_num"], 3)
        self.assertEqual(reverted["text"], "Original text")
        self.assertEqual(reverted["source"], "manual")

    def test_revert_preserves_history(self) -> None:
        """Откат не удаляет более новые версии."""
        self.manager.revert_to_version("item_001", 1)
        versions = self.manager.get_versions("item_001")
        self.assertEqual(len(versions), 3)

    def test_revert_marks_source(self) -> None:
        """Откатанная версия помечена полем reverted_from."""
        reverted = self.manager.revert_to_version("item_001", 1)
        self.assertEqual(reverted["reverted_from"], 1)

    def test_revert_nonexistent_raises(self) -> None:
        """Откат к несуществующей версии вызывает KeyError."""
        with self.assertRaises(KeyError):
            self.manager.revert_to_version("item_001", 99)


class TestTranscriptVersionManagerDiff(unittest.TestCase):
    """Тесты различия между версиями."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)
        self.manager.save_version("item_001", "Hello world", "stt_raw")
        self.manager.save_version("item_001", "Hello beautiful world", "manual")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_diff_versions_structure(self) -> None:
        """diff_versions возвращает структурированный diff."""
        result = self.manager.diff_versions("item_001", 1, 2)
        self.assertIn("item_id", result)
        self.assertIn("v1", result)
        self.assertIn("v2", result)
        self.assertIn("text_v1", result)
        self.assertIn("text_v2", result)
        self.assertIn("unified_diff", result)
        self.assertIn("added_lines", result)
        self.assertIn("removed_lines", result)

    def test_diff_versions_counts_changes(self) -> None:
        """diff_versions подсчитывает добавленные и удалённые строки."""
        result = self.manager.diff_versions("item_001", 1, 2)
        self.assertGreater(result["added_lines"], 0)

    def test_diff_versions_nonexistent_raises(self) -> None:
        """diff_versions для несуществующей версии вызывает KeyError."""
        with self.assertRaises(KeyError):
            self.manager.diff_versions("item_001", 1, 99)


class TestTranscriptVersionManagerPersistence(unittest.TestCase):
    """Тесты персистентности данных."""

    def test_persistence_survives_restart(self) -> None:
        """Версии сохраняются в NDJSON и доступны после пересоздания менеджера."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            manager1 = TranscriptVersionManager(temp_dir.name)
            manager1.save_version("item_001", "Persistent text", "manual")
            manager1.save_version("item_001", "Second version", "manual")

            manager2 = TranscriptVersionManager(temp_dir.name)
            versions = manager2.get_versions("item_001")

            self.assertEqual(len(versions), 2)
            self.assertEqual(versions[0]["text"], "Second version")
            self.assertEqual(versions[1]["text"], "Persistent text")
        finally:
            temp_dir.cleanup()

    def test_ndjson_format(self) -> None:
        """Версии сохраняются в NDJSON формате."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            manager = TranscriptVersionManager(temp_dir.name)
            manager.save_version("item_001", "Test text", "manual")

            versions_file = Path(temp_dir.name) / "transcript_versions.ndjson"
            content = versions_file.read_text(encoding="utf-8")
            self.assertIn("item_001", content)
            self.assertIn("Test text", content)
            self.assertIn("manual", content)
        finally:
            temp_dir.cleanup()


class TestTranscriptVersionManagerEmptyState(unittest.TestCase):
    """Тесты пустого состояния (нет версий)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_state_get_versions(self) -> None:
        """get_versions для пустой БД возвращает пустой список."""
        versions = self.manager.get_versions("nonexistent")
        self.assertEqual(versions, [])

    def test_empty_state_file_created(self) -> None:
        """Файл NDJSON создаётся при инициализации."""
        versions_file = Path(self.temp_dir.name) / "transcript_versions.ndjson"
        self.assertTrue(versions_file.exists())


class TestTranscriptVersionManagerIPC(unittest.TestCase):
    """Тесты IPC-обработчиков."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ipc_handle_save_transcript_version(self) -> None:
        """handle_save_transcript_version работает через IPC."""
        result = self.manager.handle_save_transcript_version({
            "item_id": "ipc_item",
            "text": "IPC text",
            "source": "stt_raw",
        })
        self.assertEqual(result["item_id"], "ipc_item")
        self.assertEqual(result["text"], "IPC text")
        self.assertEqual(result["source"], "stt_raw")

    def test_ipc_handle_save_missing_item_id_raises(self) -> None:
        """IPC обработчик требует item_id."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.handle_save_transcript_version({"text": "Text"})
        self.assertIn("item_id", str(ctx.exception))

    def test_ipc_handle_save_missing_text_raises(self) -> None:
        """IPC обработчик требует text."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.handle_save_transcript_version({"item_id": "item"})
        self.assertIn("text", str(ctx.exception))

    def test_ipc_handle_get_transcript_versions(self) -> None:
        """handle_get_transcript_versions возвращает структурированный результат."""
        self.manager.save_version("item_001", "Text 1", "manual")
        self.manager.save_version("item_001", "Text 2", "manual")
        result = self.manager.handle_get_transcript_versions({"item_id": "item_001"})
        self.assertEqual(result["item_id"], "item_001")
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["versions"]), 2)

    def test_ipc_handle_revert_transcript_version(self) -> None:
        """handle_revert_transcript_version работает через IPC."""
        self.manager.save_version("item_001", "Version 1", "manual")
        self.manager.save_version("item_001", "Version 2", "manual")
        result = self.manager.handle_revert_transcript_version({
            "item_id": "item_001",
            "version_num": 1,
        })
        self.assertEqual(result["version_num"], 3)
        self.assertEqual(result["text"], "Version 1")

    def test_ipc_handle_get_versions_missing_item_id_raises(self) -> None:
        """handle_get_transcript_versions требует item_id."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.handle_get_transcript_versions({})
        self.assertIn("item_id", str(ctx.exception))

    def test_ipc_handle_revert_missing_version_num_raises(self) -> None:
        """handle_revert_transcript_version требует version_num."""
        self.manager.save_version("item_001", "Text", "manual")
        with self.assertRaises(ValueError) as ctx:
            self.manager.handle_revert_transcript_version({"item_id": "item_001"})
        self.assertIn("version_num", str(ctx.exception))


class TestTranscriptVersionManagerDiffEdgeCases(unittest.TestCase):
    """Edge-cases для diff_versions."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_diff_identical_texts_zero_changes(self) -> None:
        """diff_versions для двух идентичных версий: added и removed равны 0."""
        self.manager.save_version("item_x", "Same text", "manual")
        self.manager.save_version("item_x", "Same text", "manual")
        result = self.manager.diff_versions("item_x", 1, 2)
        self.assertEqual(result["added_lines"], 0)
        self.assertEqual(result["removed_lines"], 0)

    def test_diff_same_version_number_raises(self) -> None:
        """diff_versions(v, v, v) — сравнение версии с собой не ошибка."""
        self.manager.save_version("item_x", "Text", "manual")
        result = self.manager.diff_versions("item_x", 1, 1)
        self.assertEqual(result["added_lines"], 0)
        self.assertEqual(result["removed_lines"], 0)

    def test_diff_returns_texts(self) -> None:
        """diff_versions включает оригинальные тексты обеих версий."""
        self.manager.save_version("item_x", "First", "stt_raw")
        self.manager.save_version("item_x", "Second", "manual")
        result = self.manager.diff_versions("item_x", 1, 2)
        self.assertEqual(result["text_v1"], "First")
        self.assertEqual(result["text_v2"], "Second")


class TestTranscriptVersionManagerRevertPersistence(unittest.TestCase):
    """Проверка что revert сохраняет reverted_from в NDJSON."""

    def test_reverted_from_persists(self) -> None:
        """reverted_from из revert_to_version сохраняется в NDJSON после перезагрузки."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            mgr1 = TranscriptVersionManager(temp_dir.name)
            mgr1.save_version("item_r", "Original", "stt_raw")
            mgr1.save_version("item_r", "Edited", "manual")
            mgr1.revert_to_version("item_r", 1)

            # Reload и проверяем что все 3 версии есть
            mgr2 = TranscriptVersionManager(temp_dir.name)
            versions = mgr2.get_versions("item_r")
            self.assertEqual(len(versions), 3)
            # Версия 3 (самая новая) должна содержать текст из версии 1
            latest = versions[0]  # sorted desc
            self.assertEqual(latest["version_num"], 3)
            self.assertEqual(latest["text"], "Original")
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
