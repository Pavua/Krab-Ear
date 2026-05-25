"""Unit-тесты для DataMigrator Krab Ear."""

from __future__ import annotations
from backend.data_migrator import (
    DataMigrator,
    MigrationResult,
    LATEST_VERSION,
    _detect_version_from_items,
    _read_ndjson,
)

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _write_ndjson(path: Path, items: list[dict]) -> None:
    """Вспомогательная функция: записывает список объектов в NDJSON-файл."""
    lines = [json.dumps(item, ensure_ascii=False) for item in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_v1_item(item_id: str = "abc", text: str = "Привет мир") -> dict:
    """v1.0-запись: без полей tags/favorite/annotation."""
    return {
        "id": item_id,
        "ts": "2024-01-01T12:00:00",
        "text": text,
        "paste_status": "ok",
    }


def _make_v2_item(item_id: str = "abc", text: str = "Привет мир") -> dict:
    """v2.0-запись: с полями tags/favorite/annotation."""
    return {
        "id": item_id,
        "ts": "2024-01-01T12:00:00",
        "text": text,
        "paste_status": "ok",
        "tags": [],
        "favorite": False,
        "annotation": "",
    }


class TestDetectVersionFromItems(unittest.TestCase):
    """Тесты функции _detect_version_from_items."""

    def test_empty_items_returns_latest(self) -> None:
        """Пустой список → текущая последняя версия."""
        self.assertEqual(_detect_version_from_items([]), LATEST_VERSION)

    def test_items_missing_tags_returns_v1(self) -> None:
        """Запись без поля tags → версия 1.0."""
        items = [_make_v1_item()]
        self.assertEqual(_detect_version_from_items(items), "1.0")

    def test_items_with_all_v2_fields_returns_v2(self) -> None:
        """Запись со всеми v2-полями → версия 2.0."""
        items = [_make_v2_item()]
        self.assertEqual(_detect_version_from_items(items), LATEST_VERSION)

    def test_mixed_items_returns_v1_if_any_missing(self) -> None:
        """Если хотя бы одна запись не имеет tags — считаем v1.0."""
        items = [_make_v2_item("x"), _make_v1_item("y")]
        self.assertEqual(_detect_version_from_items(items), "1.0")


class TestGetSchemaVersion(unittest.TestCase):
    """Тесты DataMigrator.get_schema_version."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_empty_dir_returns_latest(self) -> None:
        """Директория без history.ndjson → текущая последняя версия."""
        version = self._migrator.get_schema_version(self._tmpdir)
        self.assertEqual(version, LATEST_VERSION)

    def test_v1_data_detected_as_v1(self) -> None:
        """Записи без tags → версия 1.0."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("id1"), _make_v1_item("id2")])
        version = self._migrator.get_schema_version(self._tmpdir)
        self.assertEqual(version, "1.0")

    def test_v2_data_detected_as_v2(self) -> None:
        """Записи с tags и favorite → версия 2.0."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v2_item("id1"), _make_v2_item("id2")])
        version = self._migrator.get_schema_version(self._tmpdir)
        self.assertEqual(version, LATEST_VERSION)

    def test_tombstoned_items_excluded_from_version_check(self) -> None:
        """Удалённые записи (tombstones) не учитываются при определении версии."""
        history_path = self._tmpdir / "history.ndjson"
        tombstones_path = self._tmpdir / "history_tombstones.ndjson"

        item = _make_v1_item("del1")
        _write_ndjson(history_path, [item])
        _write_ndjson(tombstones_path, [{"id": "del1"}])

        # После исключения tombstone активных v1-записей нет → v2.0
        version = self._migrator.get_schema_version(self._tmpdir)
        self.assertEqual(version, LATEST_VERSION)


class TestCheckMigrationNeeded(unittest.TestCase):
    """Тесты DataMigrator.check_migration_needed."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_v1_data_needs_migration(self) -> None:
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item()])
        self.assertTrue(self._migrator.check_migration_needed(self._tmpdir))

    def test_v2_data_no_migration_needed(self) -> None:
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v2_item()])
        self.assertFalse(self._migrator.check_migration_needed(self._tmpdir))

    def test_empty_dir_no_migration_needed(self) -> None:
        self.assertFalse(self._migrator.check_migration_needed(self._tmpdir))


class TestGetMigrationPlan(unittest.TestCase):
    """Тесты DataMigrator.get_migration_plan."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_no_migration_plan_for_current_version(self) -> None:
        """Актуальные данные: план содержит сообщение 'не требуется'."""
        plan = self._migrator.get_migration_plan(self._tmpdir)
        self.assertEqual(len(plan), 1)
        self.assertIn("не требуется", plan[0])

    def test_v1_plan_includes_field_names(self) -> None:
        """План для v1 → v2 упоминает добавляемые поля."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("a"), _make_v1_item("b")])
        plan = self._migrator.get_migration_plan(self._tmpdir)
        full_plan = " ".join(plan)
        self.assertIn("tags", full_plan)
        self.assertIn("favorite", full_plan)

    def test_v1_plan_mentions_backup(self) -> None:
        """План содержит упоминание резервной копии."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item()])
        plan = self._migrator.get_migration_plan(self._tmpdir)
        full_plan = " ".join(plan).lower()
        self.assertIn("резерв", full_plan)

    def test_plan_includes_version_info(self) -> None:
        """План для v1 содержит номера версий."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item()])
        plan = self._migrator.get_migration_plan(self._tmpdir)
        full_plan = " ".join(plan)
        self.assertIn("1.0", full_plan)
        self.assertIn("2.0", full_plan)


class TestMigrateV1ToV2(unittest.TestCase):
    """Тесты DataMigrator.migrate — миграция v1.0 → v2.0."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_migrate_adds_missing_fields(self) -> None:
        """После миграции все записи содержат tags, favorite, annotation."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("id1"), _make_v1_item("id2")])

        self._migrator.migrate(self._tmpdir)

        items = _read_ndjson(history_path)
        for item in items:
            self.assertIn("tags", item)
            self.assertIn("favorite", item)
            self.assertIn("annotation", item)
            self.assertEqual(item["tags"], [])
            self.assertEqual(item["favorite"], False)
            self.assertEqual(item["annotation"], "")

    def test_migrate_returns_correct_counts(self) -> None:
        """MigrationResult.items_migrated = количество обновлённых записей."""
        history_path = self._tmpdir / "history.ndjson"
        items = [_make_v1_item(f"id{i}") for i in range(5)]
        _write_ndjson(history_path, items)

        result = self._migrator.migrate(self._tmpdir)
        self.assertEqual(result.items_migrated, 5)
        self.assertEqual(result.items_skipped, 0)

    def test_migrate_already_v2_no_changes(self) -> None:
        """Если данные уже в v2 — items_migrated=0, from_version==to_version."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v2_item("id1"), _make_v2_item("id2")])

        result = self._migrator.migrate(self._tmpdir)
        self.assertEqual(result.items_migrated, 0)
        self.assertEqual(result.from_version, result.to_version)

    def test_migrate_creates_backup(self) -> None:
        """Миграция всегда создаёт backup_path, даже если нечего мигрировать."""
        result = self._migrator.migrate(self._tmpdir)
        backup = Path(result.backup_path)
        self.assertTrue(backup.exists())
        self.assertTrue(backup.is_dir())

    def test_migrate_backup_contains_meta_file(self) -> None:
        """Бэкап содержит migration_meta.json."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item()])
        result = self._migrator.migrate(self._tmpdir)
        meta_file = Path(result.backup_path) / "migration_meta.json"
        self.assertTrue(meta_file.exists())

    def test_migrate_result_dataclass_fields(self) -> None:
        """MigrationResult содержит все нужные поля."""
        result = self._migrator.migrate(self._tmpdir)
        self.assertIsInstance(result, MigrationResult)
        self.assertIsNotNone(result.from_version)
        self.assertIsNotNone(result.to_version)
        self.assertIsInstance(result.items_migrated, int)
        self.assertIsInstance(result.items_skipped, int)
        self.assertIsNotNone(result.backup_path)

    def test_migrate_preserves_existing_fields(self) -> None:
        """Существующие поля записи не изменяются при миграции."""
        history_path = self._tmpdir / "history.ndjson"
        original_item = _make_v1_item("id1", "Тест сохранения полей")
        original_item["paste_status"] = "pasted"
        _write_ndjson(history_path, [original_item])

        self._migrator.migrate(self._tmpdir)

        items = _read_ndjson(history_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "Тест сохранения полей")
        self.assertEqual(items[0]["paste_status"], "pasted")

    def test_migrate_unsupported_target_version_raises(self) -> None:
        """Неподдерживаемая целевая версия выбрасывает ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.migrate(self._tmpdir, target_version="3.0")

    def test_migrate_partial_v2_items_counted_correctly(self) -> None:
        """Записи, уже имеющие v2-поля, учитываются в items_skipped."""
        history_path = self._tmpdir / "history.ndjson"
        items = [
            _make_v1_item("old1"),
            _make_v1_item("old2"),
            _make_v2_item("new1"),  # уже v2
        ]
        _write_ndjson(history_path, items)

        result = self._migrator.migrate(self._tmpdir)
        # Из 3 записей: 2 мигрируется, 1 пропускается (уже v2)
        self.assertEqual(result.items_migrated, 2)
        self.assertEqual(result.items_skipped, 1)

    def test_migrate_empty_history_is_safe(self) -> None:
        """Миграция пустой истории завершается без ошибок."""
        history_path = self._tmpdir / "history.ndjson"
        history_path.write_text("", encoding="utf-8")

        result = self._migrator.migrate(self._tmpdir)
        self.assertEqual(result.items_migrated, 0)

    def test_migrate_from_version_matches_detected(self) -> None:
        """from_version в результате совпадает с обнаруженной версией."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item()])

        current_version = self._migrator.get_schema_version(self._tmpdir)
        result = self._migrator.migrate(self._tmpdir)
        self.assertEqual(result.from_version, current_version)

    def test_schema_version_is_v2_after_migration(self) -> None:
        """После миграции get_schema_version возвращает v2.0."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("a"), _make_v1_item("b")])

        self._migrator.migrate(self._tmpdir)
        version_after = self._migrator.get_schema_version(self._tmpdir)
        self.assertEqual(version_after, "2.0")


class TestIpcHandlers(unittest.TestCase):
    """Тесты IPC-обработчиков DataMigrator."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_handle_check_migration_no_dir_raises(self) -> None:
        """handle_check_migration без data_dir → ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.handle_check_migration({})

    def test_handle_check_migration_returns_expected_keys(self) -> None:
        """handle_check_migration возвращает нужные поля."""
        result = self._migrator.handle_check_migration({"data_dir": str(self._tmpdir)})
        self.assertIn("migration_needed", result)
        self.assertIn("current_version", result)
        self.assertIn("target_version", result)
        self.assertIn("plan", result)
        self.assertIsInstance(result["plan"], list)

    def test_handle_check_migration_detects_v1(self) -> None:
        """handle_check_migration распознаёт v1-данные как требующие миграции."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item()])
        result = self._migrator.handle_check_migration({"data_dir": str(self._tmpdir)})
        self.assertTrue(result["migration_needed"])
        self.assertEqual(result["current_version"], "1.0")

    def test_handle_run_migration_no_dir_raises(self) -> None:
        """handle_run_migration без data_dir → ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.handle_run_migration({})

    def test_handle_run_migration_returns_expected_keys(self) -> None:
        """handle_run_migration возвращает нужные поля."""
        result = self._migrator.handle_run_migration({"data_dir": str(self._tmpdir)})
        self.assertIn("from_version", result)
        self.assertIn("to_version", result)
        self.assertIn("items_migrated", result)
        self.assertIn("items_skipped", result)
        self.assertIn("backup_path", result)

    def test_handle_run_migration_executes_migration(self) -> None:
        """handle_run_migration применяет миграцию к v1-данным."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("i1"), _make_v1_item("i2")])

        result = self._migrator.handle_run_migration({"data_dir": str(self._tmpdir)})
        self.assertEqual(result["from_version"], "1.0")
        self.assertEqual(result["to_version"], "2.0")
        self.assertEqual(result["items_migrated"], 2)

    def test_handle_run_migration_invalid_version_raises(self) -> None:
        """handle_run_migration с неподдерживаемой версией → ошибка."""
        with self.assertRaises(ValueError):
            self._migrator.handle_run_migration({
                "data_dir": str(self._tmpdir),
                "target_version": "99.0",
            })


class TestRollbackMigration(unittest.TestCase):
    """Тесты DataMigrator.rollback_migration — откат после миграции."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_rollback_restores_history_file(self) -> None:
        """После отката history.ndjson соответствует состоянию до миграции."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("id1"), _make_v1_item("id2")])
        original_content = history_path.read_text(encoding="utf-8")

        result = self._migrator.migrate(self._tmpdir)
        # Убеждаемся, что миграция изменила файл
        self.assertNotEqual(history_path.read_text(encoding="utf-8"), original_content)

        # Откат
        rollback = self._migrator.rollback_migration(self._tmpdir, result.backup_path)
        self.assertIn("history.ndjson", rollback["restored_files"])
        self.assertEqual(history_path.read_text(encoding="utf-8"), original_content)

    def test_rollback_invalid_backup_raises(self) -> None:
        """rollback_migration с несуществующим backup_path → ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.rollback_migration(self._tmpdir, "/nonexistent/backup")

    def test_rollback_returns_expected_keys(self) -> None:
        """rollback_migration возвращает restored_files и backup_path."""
        result = self._migrator.migrate(self._tmpdir)
        rollback = self._migrator.rollback_migration(self._tmpdir, result.backup_path)
        self.assertIn("restored_files", rollback)
        self.assertIn("backup_path", rollback)
        self.assertIsInstance(rollback["restored_files"], list)


class TestReadNdjson(unittest.TestCase):
    """Тесты вспомогательной функции _read_ndjson."""

    def test_reads_all_lines(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "test.ndjson"
        items = [{"id": str(i), "text": f"item {i}"} for i in range(5)]
        _write_ndjson(path, items)
        result = _read_ndjson(path)
        self.assertEqual(len(result), 5)

    def test_skips_empty_lines(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "test.ndjson"
        path.write_text('{"id": "1"}\n\n{"id": "2"}\n', encoding="utf-8")
        result = _read_ndjson(path)
        self.assertEqual(len(result), 2)

    def test_nonexistent_file_returns_empty(self) -> None:
        result = _read_ndjson(Path("/nonexistent/path/file.ndjson"))
        self.assertEqual(result, [])


class TestInvalidVersionHandling(unittest.TestCase):
    """Тесты на некорректные строки версии и edge-cases."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_handles_invalid_target_version_string(self) -> None:
        """Migrate с неизвестной строкой версии (не '2.0') → ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.migrate(self._tmpdir, target_version="invalid_ver")

    def test_handles_empty_target_version_string(self) -> None:
        """Migrate с пустой строкой версии → ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.migrate(self._tmpdir, target_version="")

    def test_handles_version_with_spaces(self) -> None:
        """Migrate с версией содержащей пробелы → ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.migrate(self._tmpdir, target_version=" 2.0 ")

    def test_check_migration_ipc_unknown_version_in_plan(self) -> None:
        """handle_check_migration не падает при любой версии данных в директории."""
        # Создаём историю с нестандартной структурой (нет text и нет tags)
        history_path = self._tmpdir / "history.ndjson"
        # Записи без text не считаются v1 (нет tags), при этом нет text-поля
        history_path.write_text('{"id": "x", "ts": "2024-01-01"}\n', encoding="utf-8")
        result = self._migrator.handle_check_migration({"data_dir": str(self._tmpdir)})
        # Должен вернуть dict без исключения
        self.assertIn("migration_needed", result)
        self.assertIn("current_version", result)


class TestRollbackOnMigrationFailure(unittest.TestCase):
    """Тест отката при сбое миграции."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_rollback_on_migration_failure(self) -> None:
        """Если миграция упала после создания бэкапа — откат восстанавливает файл."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("id1")])
        original_content = history_path.read_text(encoding="utf-8")

        # Сначала создаём бэкап вручную (как это делает migrate)
        backup_path = self._migrator._create_backup(self._tmpdir)

        # Симулируем ситуацию: файл "испорчен" после неудачной миграции
        history_path.write_text('{"id": "corrupted", "broken": true}\n', encoding="utf-8")

        # Откатываем
        rollback = self._migrator.rollback_migration(self._tmpdir, backup_path)
        self.assertIn("history.ndjson", rollback["restored_files"])
        self.assertEqual(history_path.read_text(encoding="utf-8"), original_content)

    def test_rollback_nonexistent_backup_raises_valueerror(self) -> None:
        """rollback_migration с несуществующим путём → ValueError."""
        with self.assertRaises(ValueError):
            self._migrator.rollback_migration(self._tmpdir, "/completely/nonexistent/backup_dir")

    def test_backup_dir_is_directory_not_file(self) -> None:
        """rollback_migration с путём к файлу (не директории) → ValueError."""
        fake_file = self._tmpdir / "not_a_dir.json"
        fake_file.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self._migrator.rollback_migration(self._tmpdir, str(fake_file))


class TestConcurrentMigration(unittest.TestCase):
    """Тест параллельного запуска миграции."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()
        # Создаём историю v1
        history_path = self._tmpdir / "history.ndjson"
        items = [_make_v1_item(f"id{i}") for i in range(10)]
        _write_ndjson(history_path, items)

    def test_concurrent_migration_does_not_corrupt_data(self) -> None:
        """Несколько одновременных migrate() не должны портить history.ndjson.

        DataMigrator не имеет внешнего POSIX-лока (использует Python tmpfile/replace),
        но атомарный rename гарантирует, что финальный файл корректен.
        """
        results: list[MigrationResult] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                result = self._migrator.migrate(self._tmpdir)
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # После всех потоков файл должен быть читаемым NDJSON
        history_path = self._tmpdir / "history.ndjson"
        self.assertTrue(history_path.exists())
        items = _read_ndjson(history_path)
        # Все items, имеющие поле text, должны иметь v2-поля
        for item in items:
            if "text" in item:
                self.assertIn("tags", item)
                self.assertIn("favorite", item)


class TestUnicodeDataPreserved(unittest.TestCase):
    """Тест сохранности Unicode-данных через миграцию."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        self._migrator = DataMigrator()

    def test_cyrillic_text_preserved(self) -> None:
        """Кириллические данные не искажаются при миграции v1→v2."""
        text = "Привет! Это тест миграции данных Krab Ear на русском языке."
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("ru_id", text)])

        self._migrator.migrate(self._tmpdir)

        items = _read_ndjson(history_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], text)

    def test_spanish_text_preserved(self) -> None:
        """Испанские символы (ñ, á, é, ü) не теряются при миграции."""
        text = "Canción de niño — España y más allá"
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("es_id", text)])

        self._migrator.migrate(self._tmpdir)

        items = _read_ndjson(history_path)
        self.assertEqual(items[0]["text"], text)

    def test_emoji_in_text_preserved(self) -> None:
        """Emoji и нестандартные Unicode символы не теряются."""
        text = "Тест 🎙️ — Krab Ear записывает 🇷🇺 и переводит 🇪🇸"
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("emoji_id", text)])

        self._migrator.migrate(self._tmpdir)

        items = _read_ndjson(history_path)
        self.assertEqual(items[0]["text"], text)

    def test_mixed_languages_in_single_item_preserved(self) -> None:
        """Смешанный текст (RU/ES/EN) в одной записи сохраняется корректно."""
        text = "Hola мир hello — code-switching тест"
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("multi_id", text)])

        self._migrator.migrate(self._tmpdir)

        items = _read_ndjson(history_path)
        self.assertEqual(items[0]["text"], text)

    def test_backup_meta_json_is_valid_utf8(self) -> None:
        """migration_meta.json в бэкапе читается как валидный UTF-8 JSON."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("u1", "Тест с кириллицей")])

        result = self._migrator.migrate(self._tmpdir)
        meta_path = Path(result.backup_path) / "migration_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertIn("files", meta)
        self.assertIn("migration_backup_ts", meta)


if __name__ == "__main__":
    unittest.main()
