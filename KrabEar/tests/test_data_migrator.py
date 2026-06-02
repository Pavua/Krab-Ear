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
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_minimal_backend_service():
    """Конструирует РЕАЛЬНУЮ BackendService с лёгкими фейками для проверки
    ЖИВОЙ таблицы диспетчеризации (W1769).

    Использует тот же приём, что и test_dispatch_complete: рекордер/транскрайбер/
    транслятор подменяются фейками через конструктор; тяжёлые ML-модели не грузятся
    (AudioEngine не инстанцируется без вызова транскрипции).
    """
    import tempfile
    import numpy as np
    from backend.state_store import StateStore
    from backend.service import BackendService
    from backend.translator import TranslationResult

    class _FakeRecorder:
        is_recording = False
        sample_rate = 16000

        def start(self):
            self.is_recording = True
            return True

        def stop(self, timeout_sec=3.0, trim_tail_ms=0):
            if not self.is_recording:
                return None
            self.is_recording = False
            return np.zeros(16000, dtype=np.float32), 1.0

    class _FakeEngine:
        _last_llm_diff = None
        _llm_rewriter = None
        quality_profile = "balanced"
        current_model = "fake-model"

        def _resolve_diarization_device(self) -> str:
            return "cpu"

    class _FakeTranscriber:
        def __init__(self):
            self.engine = _FakeEngine()

        def transcribe(self, *a, **kw):
            return "fake"

    class _FakeTranslator:
        last_mode = "off"

        def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
            return TranslationResult(
                text="", status="not_requested", source_lang="",
                target_lang="", mode=mode, engine="fake",
            )

    tmp = tempfile.mkdtemp()
    store = StateStore(Path(tmp) / "data")
    return BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )


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
    """Тесты IPC-обработчиков DataMigrator (W1761: data_dir из запроса игнорируется)."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        # W1761: DataMigrator инициализируется с data_dir (единственно допустимый путь)
        self._migrator = DataMigrator(data_dir=self._tmpdir)

    def test_handle_check_migration_no_configured_dir_raises(self) -> None:
        """handle_check_migration без data_dir в конструкторе → RuntimeError."""
        migrator_no_dir = DataMigrator()
        with self.assertRaises(RuntimeError):
            migrator_no_dir.handle_check_migration({})

    def test_handle_check_migration_returns_expected_keys(self) -> None:
        """handle_check_migration возвращает нужные поля (data_dir из params игнорируется)."""
        # Передаём в params иной путь — он должен быть проигнорирован
        result = self._migrator.handle_check_migration({"data_dir": "/tmp/evil_krab_migrate_test"})
        self.assertIn("migration_needed", result)
        self.assertIn("current_version", result)
        self.assertIn("target_version", result)
        self.assertIn("plan", result)
        self.assertIsInstance(result["plan"], list)

    def test_handle_check_migration_detects_v1(self) -> None:
        """handle_check_migration распознаёт v1-данные как требующие миграции."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item()])
        # data_dir из params игнорируется — мигратор читает из self._tmpdir
        result = self._migrator.handle_check_migration({"data_dir": "/tmp/evil_krab_migrate_test"})
        self.assertTrue(result["migration_needed"])
        self.assertEqual(result["current_version"], "1.0")

    def test_handle_run_migration_no_configured_dir_raises(self) -> None:
        """handle_run_migration без data_dir в конструкторе → RuntimeError."""
        migrator_no_dir = DataMigrator()
        with self.assertRaises(RuntimeError):
            migrator_no_dir.handle_run_migration({})

    def test_handle_run_migration_returns_expected_keys(self) -> None:
        """handle_run_migration возвращает нужные поля (data_dir из params игнорируется)."""
        result = self._migrator.handle_run_migration({"data_dir": "/tmp/evil_krab_migrate_test"})
        self.assertIn("from_version", result)
        self.assertIn("to_version", result)
        self.assertIn("items_migrated", result)
        self.assertIn("items_skipped", result)
        self.assertIn("backup_path", result)

    def test_handle_run_migration_executes_migration(self) -> None:
        """handle_run_migration применяет миграцию к v1-данным в сконфигурированной директории."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("i1"), _make_v1_item("i2")])

        # data_dir из params — произвольный путь, игнорируется
        result = self._migrator.handle_run_migration({"data_dir": "/tmp/evil_krab_migrate_test"})
        self.assertEqual(result["from_version"], "1.0")
        self.assertEqual(result["to_version"], "2.0")
        self.assertEqual(result["items_migrated"], 2)

    def test_handle_run_migration_invalid_version_raises(self) -> None:
        """handle_run_migration с неподдерживаемой версией → ошибка."""
        with self.assertRaises(ValueError):
            self._migrator.handle_run_migration({"target_version": "99.0"})

    def test_handle_run_migration_does_not_write_to_arbitrary_path(self) -> None:
        """W1761 regression: run_migration с data_dir='/tmp/evil_krab_migrate_test' НЕ пишет туда.

        Вектор: злонамеренный локальный IPC-клиент передаёт data_dir за пределами
        директории данных приложения. После фикса W1761 этот путь игнорируется —
        мигратор работает только с закреплённым self._data_dir.
        """
        evil_dir = Path("/tmp/evil_krab_migrate_test")
        if evil_dir.exists():
            import shutil as _shutil
            _shutil.rmtree(evil_dir)

        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("sec1")])

        # Запрос с произвольным data_dir
        result = self._migrator.handle_run_migration({"data_dir": str(evil_dir)})

        # Evil dir не должна быть создана
        self.assertFalse(
            evil_dir.exists(),
            "Уязвимость W1761: run_migration записал файлы в произвольную директорию",
        )

        # Миграция должна была отработать на сконфигурированном self._tmpdir
        self.assertEqual(result["from_version"], "1.0")
        self.assertEqual(result["to_version"], "2.0")
        # backup_path должен быть внутри self._tmpdir (resolve убирает symlink /tmp→/private/tmp)
        resolved_backup = Path(result["backup_path"]).resolve()
        resolved_tmpdir = self._tmpdir.resolve()
        self.assertTrue(
            resolved_backup.is_relative_to(resolved_tmpdir),
            f"backup_path {resolved_backup} должен быть внутри {resolved_tmpdir}",
        )


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
        # W1761: отдельный мигратор с data_dir для IPC-вызовов
        self._migrator_with_dir = DataMigrator(data_dir=self._tmpdir)

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
        # W1761: используем мигратор с data_dir; params-путь игнорируется
        result = self._migrator_with_dir.handle_check_migration({})
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


class DataMigratorRollbackIPCTestCase(unittest.TestCase):
    """Тесты IPC-обёртки handle_rollback_migration (W1026 F3, W1761)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._tmpdir = Path(self._tmp.name)
        # W1761: инициализируем с data_dir, чтобы IPC-обработчики использовали его
        self._migrator = DataMigrator(data_dir=self._tmpdir)

    def _make_v1_history(self) -> None:
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("id1", "Тест")])

    def test_handle_rollback_migration_restores_files(self) -> None:
        """handle_rollback_migration восстанавливает файлы из резервной копии."""
        self._make_v1_history()
        result = self._migrator.migrate(self._tmpdir)
        backup_path = result.backup_path

        # Симулируем повреждённую историю после миграции
        corrupt_line = json.dumps({"id": "bad", "text": "corrupted"})
        (self._tmpdir / "history.ndjson").write_text(corrupt_line + "\n", encoding="utf-8")

        # data_dir в params игнорируется (W1761); backup_path должен быть внутри backups/
        rollback_result = self._migrator.handle_rollback_migration({
            "backup_path": backup_path,
        })

        self.assertIn("restored_files", rollback_result)
        self.assertIn("history.ndjson", rollback_result["restored_files"])
        self.assertEqual(rollback_result["backup_path"], backup_path)

    def test_handle_rollback_migration_missing_backup_path_raises(self) -> None:
        """handle_rollback_migration требует backup_path."""
        with self.assertRaises(ValueError):
            self._migrator.handle_rollback_migration({})

    def test_handle_rollback_migration_invalid_backup_path_raises(self) -> None:
        """handle_rollback_migration выбрасывает (ValueError/RuntimeError) при несуществующем backup."""
        with self.assertRaises((ValueError, RuntimeError)):
            self._migrator.handle_rollback_migration({
                "backup_path": str(self._tmpdir / "backups" / "nonexistent_backup"),
            })

    def test_handle_rollback_migration_traversal_outside_backups_raises(self) -> None:
        """W1761: backup_path за пределами <data_dir>/backups/ → RuntimeError."""
        with self.assertRaises(RuntimeError):
            self._migrator.handle_rollback_migration({
                "backup_path": "/tmp/evil_krab_rollback_test",
            })


class TestWave1767Hardening(unittest.TestCase):
    """Тесты четырёх MED-исправлений Wave 1767 (hardening data_migrator).

    #11 — rollback_migration wired в dispatch table
    #12 — tmp-файл удаляется при исключении между write и replace
    #13 — rollback_migration удерживает history.lock при восстановлении
    #14 — _create_backup копирует history_text_updates.ndjson + history_purged_ids.ndjson
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._tmpdir = Path(self._tmp.name)

    # ------------------------------------------------------------------
    # #11: rollback_migration доступен через dispatch table
    # ------------------------------------------------------------------

    def test_rollback_migration_in_dispatch_table(self) -> None:
        """#11: ЖИВАЯ BackendService._dispatch_table содержит 'rollback_migration'
        и вызов его диспетчеризует handle_rollback_migration DataMigrator'а.

        W1769: диспетчеризация консолидирована инлайн в service.py — единственный
        источник истины (ipc_dispatch.py удалён). Раньше rollback_migration был
        зарегистрирован ТОЛЬКО в мёртвом ipc_dispatch.py, т.е. недостижим в
        production; теперь он в живой таблице.
        """
        svc = _build_minimal_backend_service()

        class _FakeDataMigrator:
            called_with: dict | None = None

            def handle_rollback_migration(self, params: dict) -> dict:
                self.called_with = params
                return {"restored_files": [], "backup_path": "stub"}

            def handle_check_migration(self, params: dict) -> dict:
                return {}

            def handle_run_migration(self, params: dict) -> dict:
                return {}

        fake_migrator = _FakeDataMigrator()
        svc._data_migrator = fake_migrator
        # Таблица строится в __init__ из self._data_migrator; пересобираем после
        # подмены на фейк, чтобы проверить именно ЖИВУЮ логику построения.
        svc._dispatch_table = svc._build_dispatch_table()

        # Ключ обязан присутствовать в ЖИВОЙ таблице
        self.assertIn("rollback_migration", svc._dispatch_table, (
            "#11: 'rollback_migration' не найден в BackendService._dispatch_table; "
            "добавьте запись в service.py::_build_dispatch_table"
        ))

        # Вызов через ЖИВУЮ таблицу диспетчеризует именно handle_rollback_migration
        params = {"backup_path": "/stub/backup"}
        result = svc._dispatch_table["rollback_migration"](params)
        self.assertEqual(fake_migrator.called_with, params,
                         "#11: dispatch table не вызвал handle_rollback_migration")
        self.assertIn("restored_files", result)

        # И через публичный handle_request — полный путь диспетчеризации
        fake_migrator.called_with = None
        resp = svc.handle_request({
            "id": "rb1", "method": "rollback_migration",
            "params": {"backup_path": "/stub/backup2"},
        })
        self.assertTrue(resp.get("ok"), f"rollback_migration вернул ошибку: {resp}")
        self.assertEqual(fake_migrator.called_with, {"backup_path": "/stub/backup2"})

    # ------------------------------------------------------------------
    # #12: tmp-файл удаляется при исключении в write/replace
    # ------------------------------------------------------------------

    def test_tmp_file_removed_on_write_exception(self) -> None:
        """#12: если replace() бросает исключение, history.ndjson.migration_tmp
        не остаётся на диске."""
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("id1")])

        migrator = DataMigrator()

        # Патчим Path.replace чтобы симулировать сбой после write
        original_replace = Path.replace

        def _fail_replace(self_path, target):
            # Заменяем только вызовы для migration_tmp пути
            if "migration_tmp" in str(self_path):
                raise OSError("симуляция сбоя atomic replace")
            return original_replace(self_path, target)

        with patch.object(Path, "replace", _fail_replace):
            with self.assertRaises(OSError):
                migrator.migrate(self._tmpdir)

        # После исключения tmp-файл не должен существовать
        tmp_path = history_path.with_suffix(".ndjson.migration_tmp")
        self.assertFalse(
            tmp_path.exists(),
            "#12: tmp-файл migration_tmp остался на диске после исключения"
        )

    # ------------------------------------------------------------------
    # #13: rollback_migration удерживает history.lock
    # ------------------------------------------------------------------

    def test_rollback_acquires_history_lock(self) -> None:
        """#13: rollback_migration захватывает history.lock (flock LOCK_EX) вокруг
        операций восстановления файлов."""
        import fcntl as _fcntl

        # Создаём history и backup для отката
        history_path = self._tmpdir / "history.ndjson"
        _write_ndjson(history_path, [_make_v1_item("id1")])
        migrator = DataMigrator()
        result = migrator.migrate(self._tmpdir)

        flock_calls: list[int] = []
        original_flock = _fcntl.flock

        def _spy_flock(fd, op):
            flock_calls.append(op)
            return original_flock(fd, op)

        with patch.object(_fcntl, "flock", side_effect=_spy_flock):
            migrator.rollback_migration(self._tmpdir, result.backup_path)

        # Должны быть LOCK_EX и LOCK_UN
        self.assertIn(_fcntl.LOCK_EX, flock_calls,
                      "#13: rollback_migration не захватывает LOCK_EX на history.lock")
        self.assertIn(_fcntl.LOCK_UN, flock_calls,
                      "#13: rollback_migration не снимает LOCK_UN после восстановления")

    # ------------------------------------------------------------------
    # #14: _create_backup включает history_text_updates и history_purged_ids
    # ------------------------------------------------------------------

    def test_backup_includes_text_updates_and_purged_ids(self) -> None:
        """#14: _create_backup копирует history_text_updates.ndjson и
        history_purged_ids.ndjson когда они существуют."""
        # Создаём оба companion-файла
        (self._tmpdir / "history_text_updates.ndjson").write_text(
            '{"id": "u1", "text": "обновлённый текст"}\n', encoding="utf-8"
        )
        (self._tmpdir / "history_purged_ids.ndjson").write_text(
            '{"id": "p1"}\n', encoding="utf-8"
        )

        migrator = DataMigrator()
        backup_path = migrator._create_backup(self._tmpdir)
        backup_dir = Path(backup_path)

        self.assertTrue(
            (backup_dir / "history_text_updates.ndjson").exists(),
            "#14: _create_backup не скопировал history_text_updates.ndjson"
        )
        self.assertTrue(
            (backup_dir / "history_purged_ids.ndjson").exists(),
            "#14: _create_backup не скопировал history_purged_ids.ndjson"
        )

        # Содержимое должно совпадать
        self.assertEqual(
            (backup_dir / "history_text_updates.ndjson").read_text(encoding="utf-8"),
            '{"id": "u1", "text": "обновлённый текст"}\n',
        )
        self.assertEqual(
            (backup_dir / "history_purged_ids.ndjson").read_text(encoding="utf-8"),
            '{"id": "p1"}\n',
        )

    def test_backup_skips_missing_companion_files(self) -> None:
        """#14: _create_backup не падает если companion-файлы отсутствуют."""
        # Убеждаемся, что companion-файлов нет
        for fname in ("history_text_updates.ndjson", "history_purged_ids.ndjson"):
            p = self._tmpdir / fname
            if p.exists():
                p.unlink()

        migrator = DataMigrator()
        # Не должно бросать исключение
        backup_path = migrator._create_backup(self._tmpdir)
        backup_dir = Path(backup_path)

        # Файлы отсутствуют в backup — но backup_dir создан без ошибок
        self.assertFalse((backup_dir / "history_text_updates.ndjson").exists())
        self.assertFalse((backup_dir / "history_purged_ids.ndjson").exists())
        # meta-файл при этом есть
        self.assertTrue((backup_dir / "migration_meta.json").exists())


class _StubEngine:
    """Минимальный stub AudioEngine для тестов DataMigratorStartupWiringTestCase."""
    quality_profile: str = "balanced"
    current_model: str = "stub-model"
    _llm_rewriter = None
    _settings_get = None

    def _resolve_diarization_device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        pass


class _StubTranscriber:
    """Минимальный stub Transcriber для тестов DataMigratorStartupWiringTestCase."""

    def __init__(self) -> None:
        self.engine = _StubEngine()
        self._error_bus = None

    def transcribe(self, audio, **kw) -> str:
        return "test transcription"

    def transcribe_preview(self, audio, **kw) -> str:
        return "preview"


class _StubRecorder:
    """Минимальный stub AudioRecorder."""
    is_recording = False

    def start(self) -> None:
        pass

    def stop(self) -> bytes:
        return b""


class _StubTranslator:
    """Минимальный stub Translator."""

    def translate(self, text, **kw):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text,
            status="ok",
            source_lang="ru",
            target_lang="ru",
            mode="off",
            engine="stub",
        )


class DataMigratorStartupWiringTestCase(unittest.TestCase):
    """Тесты что BackendService запускает миграцию при старте если v1.0 данные (W1026 F1)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write_v1_history(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        history_path = data_dir / "history.ndjson"
        v1_item = {"id": "item1", "text": "Привет мир", "timestamp": 1700000000.0}
        _write_ndjson(history_path, [v1_item])

    def _make_service(self, data_dir: Path):
        from backend.state_store import StateStore
        from backend.service import BackendService
        store = StateStore(data_dir)
        return BackendService(
            store=store,
            recorder=_StubRecorder(),
            transcriber=_StubTranscriber(),
            translator=_StubTranslator(),
        )

    def test_data_migrator_runs_at_startup_when_needed(self) -> None:
        """BackendService автоматически мигрирует v1.0 данные при инициализации."""
        data_dir = Path(self._tmp.name) / "data"
        self._write_v1_history(data_dir)

        # Verify pre-migration state has no v2.0 fields
        history_path = data_dir / "history.ndjson"
        raw_items = _read_ndjson(history_path)
        self.assertEqual(len(raw_items), 1)
        self.assertNotIn("tags", raw_items[0])
        self.assertNotIn("favorite", raw_items[0])

        self._make_service(data_dir)

        # After __init__, history.ndjson should have v2.0 fields
        migrated_items = _read_ndjson(history_path)
        self.assertEqual(len(migrated_items), 1)
        self.assertIn("tags", migrated_items[0])
        self.assertIn("favorite", migrated_items[0])
        self.assertIn("annotation", migrated_items[0])
        self.assertEqual(migrated_items[0]["tags"], [])
        self.assertFalse(migrated_items[0]["favorite"])

    def test_data_migrator_skipped_when_already_v2(self) -> None:
        """BackendService не перезаписывает v2.0 данные при инициализации."""
        data_dir = Path(self._tmp.name) / "data2"
        data_dir.mkdir(parents=True, exist_ok=True)
        history_path = data_dir / "history.ndjson"

        # Write v2.0 item (already has tags/favorite)
        v2_item = {
            "id": "item2",
            "text": "Уже v2",
            "timestamp": 1700000001.0,
            "tags": ["test"],
            "favorite": True,
            "annotation": "ok",
        }
        _write_ndjson(history_path, [v2_item])

        self._make_service(data_dir)

        # Data should be unchanged
        items_after = _read_ndjson(history_path)
        self.assertEqual(len(items_after), 1)
        self.assertEqual(items_after[0]["tags"], ["test"])
        self.assertTrue(items_after[0]["favorite"])

    def test_rollback_migration_ipc_registered(self) -> None:
        """rollback_migration зарегистрирован как IPC handler в BackendService."""
        data_dir = Path(self._tmp.name) / "data3"
        data_dir.mkdir(parents=True, exist_ok=True)

        svc = self._make_service(data_dir)

        # Call rollback_migration IPC — should fail with ValueError (no valid backup)
        # but handler must be registered (not "unknown method" -32601)
        resp = svc.handle_request({
            "id": "t1",
            "method": "rollback_migration",
            "params": {"data_dir": str(data_dir), "backup_path": "/nonexistent"},
        })
        # Must NOT return "unknown method" error
        self.assertNotEqual(resp.get("error", {}).get("code"), -32601)


if __name__ == "__main__":
    unittest.main()
