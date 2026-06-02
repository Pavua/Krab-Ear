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

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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


class TestTranscriptVersionManagerWave103(unittest.TestCase):
    """Wave 103 — обязательные test cases по спецификации задачи."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_save_initial_version(self) -> None:
        """Первая версия получает version_num=1."""
        result = self.manager.save_version("item_init", "Initial text", "stt_raw")
        self.assertEqual(result["version_num"], 1)
        self.assertEqual(result["item_id"], "item_init")
        self.assertEqual(result["text"], "Initial text")
        self.assertEqual(result["source"], "stt_raw")

    def test_save_revision_increments_version(self) -> None:
        """Каждая следующая версия увеличивает version_num на 1."""
        v1 = self.manager.save_version("item_inc", "Text 1", "stt_raw")
        v2 = self.manager.save_version("item_inc", "Text 2", "manual")
        v3 = self.manager.save_version("item_inc", "Text 3", "llm_rewrite")
        self.assertEqual(v1["version_num"], 1)
        self.assertEqual(v2["version_num"], 2)
        self.assertEqual(v3["version_num"], 3)

    def test_get_version_by_id(self) -> None:
        """get_version возвращает конкретную версию по (item_id, version_num)."""
        self.manager.save_version("item_vid", "Version A", "stt_raw")
        self.manager.save_version("item_vid", "Version B", "manual")
        result = self.manager.get_version("item_vid", 1)
        self.assertEqual(result["version_num"], 1)
        self.assertEqual(result["text"], "Version A")
        result2 = self.manager.get_version("item_vid", 2)
        self.assertEqual(result2["version_num"], 2)
        self.assertEqual(result2["text"], "Version B")

    def test_diff_two_versions(self) -> None:
        """diff_versions возвращает корректный diff между двумя версиями."""
        self.manager.save_version("item_diff", "Hello world", "stt_raw")
        self.manager.save_version("item_diff", "Hello beautiful world", "manual")
        diff = self.manager.diff_versions("item_diff", 1, 2)
        self.assertEqual(diff["item_id"], "item_diff")
        self.assertEqual(diff["v1"], 1)
        self.assertEqual(diff["v2"], 2)
        self.assertEqual(diff["text_v1"], "Hello world")
        self.assertEqual(diff["text_v2"], "Hello beautiful world")
        self.assertIsInstance(diff["unified_diff"], list)
        self.assertGreater(diff["added_lines"], 0)

    def test_rollback_to_version(self) -> None:
        """revert_to_version создаёт новую версию с текстом из целевой."""
        self.manager.save_version("item_rb", "Original text", "stt_raw")
        self.manager.save_version("item_rb", "Edited text", "manual")
        self.manager.save_version("item_rb", "Further edited", "llm_rewrite")
        reverted = self.manager.revert_to_version("item_rb", 1)
        self.assertEqual(reverted["text"], "Original text")
        self.assertEqual(reverted["version_num"], 4)
        self.assertEqual(reverted["reverted_from"], 1)
        # History не удалена — все 4 версии сохранены
        all_versions = self.manager.get_versions("item_rb")
        self.assertEqual(len(all_versions), 4)

    def test_unicode_text_preserved(self) -> None:
        """Юникод (кириллица, иероглифы, арабский, эмодзи) сохраняется без искажений."""
        unicode_text = "Привет мир! 你好世界 مرحبا بالعالم 🌍🎉"
        result = self.manager.save_version("item_uni", unicode_text, "manual")
        self.assertEqual(result["text"], unicode_text)
        retrieved = self.manager.get_version("item_uni", 1)
        self.assertEqual(retrieved["text"], unicode_text)
        # Персистентность: перезагружаем менеджер и проверяем
        manager2 = TranscriptVersionManager(self.temp_dir.name)
        versions = manager2.get_versions("item_uni")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["text"], unicode_text)

    def test_concurrent_save_no_lost_writes(self) -> None:
        """Параллельные save_version не теряют записи и нумерация монотонна."""
        errors: list[Exception] = []
        num_threads = 10

        def worker(idx: int) -> None:
            try:
                self.manager.save_version("item_conc", f"Text from thread {idx}", "manual")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки потоков: {errors}")
        versions = self.manager.get_versions("item_conc")
        self.assertEqual(len(versions), num_threads, "Все записи должны быть сохранены")
        version_nums = sorted(v["version_num"] for v in versions)
        self.assertEqual(version_nums, list(range(1, num_threads + 1)),
                         "version_num должны быть монотонными без пропусков")


class TestTranscriptVersioningW1410(unittest.TestCase):
    """W1410 F1+F2: empty guard, size cap, reverted_from persistence."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_save_version_skips_empty_text(self) -> None:
        """save_version с пустым текстом возвращает None и ничего не пишет в NDJSON."""
        result_empty = self.manager.save_version("item_e", "", "manual")
        self.assertIsNone(result_empty)

        result_blank = self.manager.save_version("item_e", "   ", "manual")
        self.assertIsNone(result_blank)

        # Убеждаемся, что никакой записи в NDJSON не создано
        versions = self.manager.get_versions("item_e")
        self.assertEqual(versions, [])

    def test_save_version_truncates_too_large_text(self) -> None:
        """W1563: save_version raises ValueError for text exceeding _MAX_TEXT_BYTES.

        W1410 originally expected truncation with [TRUNCATED] suffix.
        W1563 changed to ValueError (explicit reject) for security/consistency.
        """
        from backend.transcript_versioning import _MAX_TEXT_BYTES

        # Генерируем строку чуть больше лимита
        large_text = "A" * (_MAX_TEXT_BYTES + 500)

        # W1563: save_version raises ValueError for oversized text
        with self.assertRaises(ValueError) as ctx:
            self.manager.save_version("item_big", large_text, "manual")

        self.assertIn("_MAX_TEXT_BYTES", str(ctx.exception))

        # Nothing should have been persisted
        versions = self.manager.get_versions("item_big")
        self.assertEqual(len(versions), 0)

    def test_revert_persists_reverted_from_field(self) -> None:
        """revert_to_version включает reverted_from в сохранённую NDJSON-запись."""
        import json as _json
        from pathlib import Path

        self.manager.save_version("item_rv", "Original", "stt_raw")
        self.manager.save_version("item_rv", "Edited", "manual")
        reverted = self.manager.revert_to_version("item_rv", 1)

        # Возвращаемый dict должен содержать reverted_from
        self.assertEqual(reverted["reverted_from"], 1)

        # NDJSON файл должен содержать reverted_from в последней строке
        versions_file = Path(self.temp_dir.name) / "transcript_versions.ndjson"
        lines = [ln.strip() for ln in versions_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        last_record = _json.loads(lines[-1])
        self.assertIn("reverted_from", last_record)
        self.assertEqual(last_record["reverted_from"], 1)

    def test_reverted_from_visible_after_reload(self) -> None:
        """reverted_from поле видно в get_versions после перезагрузки менеджера."""
        self.manager.save_version("item_reload", "Original", "stt_raw")
        self.manager.save_version("item_reload", "Edited", "manual")
        self.manager.revert_to_version("item_reload", 1)

        # Перезагружаем менеджер с того же data_dir
        reloaded = TranscriptVersionManager(self.temp_dir.name)
        versions = reloaded.get_versions("item_reload")

        # Всего 3 версии: Original, Edited, Reverted
        self.assertEqual(len(versions), 3)
        # Версия 3 (самая новая, первая при sort desc) должна иметь reverted_from
        newest = versions[0]
        self.assertEqual(newest["version_num"], 3)
        self.assertIn("reverted_from", newest)
        self.assertEqual(newest["reverted_from"], 1)


class TestTranscriptVersioningAtomicRewriteW1770(unittest.TestCase):
    """W1770: атомарность перезаписи NDJSON (data-integrity)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)
        self.versions_path = Path(self.temp_dir.name) / "transcript_versions.ndjson"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _read_records(self) -> list[dict]:
        records = []
        for line in self.versions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records

    def test_crash_mid_rewrite_keeps_original_file_intact(self) -> None:
        """Краш в момент перезаписи (os.replace бросает) не повреждает исходный файл.

        Fail-before/pass-after: старый не-атомарный write_text усекал файл ДО записи
        нового содержимого → при краше история версий пропадала. Новый атомарный
        путь (tmp+fsync+replace) пишет во временный файл и падает на replace —
        оригинальный transcript_versions.ndjson остаётся целым и полностью читаемым.
        """
        # Готовим устойчивое состояние: 2 записи у разных item_id.
        self.manager.save_version("keep_me", "Текст, который должен выжить", "stt_raw")
        self.manager.save_version("drop_me", "Версия для удаления", "manual")

        original_bytes = self.versions_path.read_bytes()
        original_records = self._read_records()
        self.assertEqual(len(original_records), 2)

        # delete_versions_for("drop_me") внутри вызывает _rewrite_all → os.replace.
        # Патчим os.replace в модуле, чтобы симулировать краш ровно в момент подмены.
        with patch(
            "backend.transcript_versioning.os.replace",
            side_effect=OSError("simulated crash during atomic replace"),
        ):
            with self.assertRaises(OSError):
                self.manager.delete_versions_for("drop_me")

        # Главная проверка: исходный файл НЕ усечён и НЕ повреждён.
        self.assertTrue(self.versions_path.exists())
        self.assertEqual(
            self.versions_path.read_bytes(),
            original_bytes,
            "После краша на os.replace оригинальный NDJSON должен остаться байт-в-байт целым",
        )
        # И он по-прежнему валиден: обе версии читаются.
        recovered = self._read_records()
        self.assertEqual(len(recovered), 2)
        ids = {r["item_id"] for r in recovered}
        self.assertEqual(ids, {"keep_me", "drop_me"})

        # tmp-мусор после неудачного replace убран finally-блоком.
        tmp_path = self.versions_path.with_suffix(".ndjson.tmp")
        self.assertFalse(tmp_path.exists(), "tmp-файл должен быть удалён после неудачного replace")

    def test_atomic_rewrite_uses_tmp_then_replace(self) -> None:
        """Перезапись действительно идёт через .ndjson.tmp + os.replace (не write_text)."""
        self.manager.save_version("item_x", "Версия 1", "stt_raw")
        self.manager.save_version("item_y", "Версия 2", "manual")

        seen_replace = {"called": False, "src": None, "dst": None}
        real_replace = os.replace

        def _spy_replace(src, dst, *a, **kw):
            seen_replace["called"] = True
            seen_replace["src"] = str(src)
            seen_replace["dst"] = str(dst)
            return real_replace(src, dst, *a, **kw)

        with patch("backend.transcript_versioning.os.replace", side_effect=_spy_replace):
            self.manager.delete_versions_for("item_x")

        self.assertTrue(seen_replace["called"], "Перезапись должна вызывать os.replace")
        self.assertTrue(seen_replace["src"].endswith(".ndjson.tmp"))
        self.assertTrue(seen_replace["dst"].endswith("transcript_versions.ndjson"))
        # Удаление состоялось: остаётся только item_y.
        remaining = {r["item_id"] for r in self._read_records()}
        self.assertEqual(remaining, {"item_y"})


class TestTranscriptVersioningClearAllW1770(unittest.TestCase):
    """W1770: публичный clear_all() — безусловный privacy-wipe всего хранилища."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)
        self.versions_path = Path(self.temp_dir.name) / "transcript_versions.ndjson"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_clear_all_empties_entire_store(self) -> None:
        """clear_all() удаляет ВСЕ версии (включая orphan-записи) и возвращает их число."""
        self.manager.save_version("item_a", "A1", "stt_raw")
        self.manager.save_version("item_a", "A2", "manual")
        self.manager.save_version("item_b", "B1", "import")
        # Версия orphan-записи (item_b удалён из истории, но версии ещё на диске).
        self.assertEqual(len(self.manager.get_versions("item_a")), 2)

        removed = self.manager.clear_all()

        self.assertEqual(removed, 3)
        # Файл пуст (никаких строк), но существует.
        self.assertTrue(self.versions_path.exists())
        self.assertEqual(self.versions_path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.manager.get_versions("item_a"), [])
        self.assertEqual(self.manager.get_versions("item_b"), [])

    def test_clear_all_idempotent_on_empty_store(self) -> None:
        """Повторный clear_all() на пустом хранилище — no-op, без исключений, возвращает 0."""
        self.assertEqual(self.manager.clear_all(), 0)
        self.manager.save_version("item_a", "A1", "stt_raw")
        self.assertEqual(self.manager.clear_all(), 1)
        self.assertEqual(self.manager.clear_all(), 0)

    def test_add_get_still_works_after_clear_all(self) -> None:
        """После clear_all() обычный цикл save/get версий продолжает работать."""
        self.manager.save_version("item_a", "Старое", "stt_raw")
        self.manager.clear_all()

        result = self.manager.save_version("item_a", "Новое после очистки", "manual")
        # Нумерация версий начинается заново с 1 (файл пуст).
        self.assertEqual(result["version_num"], 1)
        self.assertEqual(result["text"], "Новое после очистки")

        versions = self.manager.get_versions("item_a")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["text"], "Новое после очистки")
        fetched = self.manager.get_version("item_a", 1)
        self.assertEqual(fetched["source"], "manual")


if __name__ == "__main__":
    unittest.main()
