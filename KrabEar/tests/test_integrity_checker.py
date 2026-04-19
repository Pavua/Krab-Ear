"""Тесты IntegrityChecker — проверка и восстановление целостности данных Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.integrity_checker import IntegrityChecker, IntegrityReport, RepairResult

import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class IntegrityCheckerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.checker = IntegrityChecker()
        self.store = StateStore(self.data_dir)

    def test_check_integrity_empty_dir_returns_report(self) -> None:
        """Пустая директория → отчёт с нулевыми счётчиками."""
        report = self.checker.check_integrity(self.data_dir)
        self.assertIsInstance(report, IntegrityReport)
        self.assertIn(report.status, ("ok", "warnings", "errors"))

    def test_check_integrity_with_valid_items(self) -> None:
        """Валидное хранилище → статус ok или warnings без ошибок по JSON."""
        self.store.add_history_item(text="тест целостности", paste_status="ok")
        self.store.add_history_item(text="второй элемент", paste_status="ok")
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 0)
        self.assertGreaterEqual(report.total_items, 2)

    def test_check_integrity_detects_invalid_json(self) -> None:
        """Невалидный JSON в history.ndjson обнаруживается как ошибка."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id": "1", "ts": "2024-01-01T00:00:00", "text": "ok", "type": "item"}\n'
            'NOT VALID JSON LINE\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        self.assertGreater(report.invalid_json_lines, 0)
        self.assertIn(report.status, ("warnings", "errors"))

    def test_report_has_checks_list(self) -> None:
        """IntegrityReport содержит список checks."""
        report = self.checker.check_integrity(self.data_dir)
        self.assertIsInstance(report.checks, list)

    def test_repair_returns_repair_result(self) -> None:
        """repair() возвращает RepairResult с полями fixed/skipped/details."""
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertIsInstance(result, RepairResult)
        self.assertIsInstance(result.fixed, int)
        self.assertIsInstance(result.skipped, int)
        self.assertIsInstance(result.details, list)

    def test_repair_fixes_invalid_json(self) -> None:
        """repair() удаляет невалидные JSON-строки из history.ndjson."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id": "1", "ts": "2024-01-01T00:00:00", "text": "ok", "type": "item"}\n'
            'NOT VALID JSON\n'
            '{"id": "2", "ts": "2024-01-02T00:00:00", "text": "ok2", "type": "item"}\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertGreaterEqual(result.fixed, 0)
        lines = history_path.read_text(encoding="utf-8").strip().splitlines()
        for line in lines:
            if line.strip():
                json.loads(line)

    def test_repair_clean_store_is_noop(self) -> None:
        """Чистое хранилище — repair() ничего не меняет."""
        self.store.add_history_item(text="clean", paste_status="ok")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertEqual(result.fixed, 0)

    def test_check_integrity_detects_missing_required_fields(self) -> None:
        """Items missing required fields (id, ts, text) детектируются как ошибка."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id": "1", "ts": "2024-01-01T00:00:00", "text": "ok"}\n'
            '{"id": "2", "ts": "2024-01-02T00:00:00"}\n'
            '{"ts": "2024-01-03T00:00:00", "text": "ok"}\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        required_check = next(
            (c for c in report.checks if c.name == "required_fields"), None
        )
        self.assertIsNotNone(required_check)
        self.assertIn(required_check.status, ("error", "warning"))

    def test_check_integrity_detects_orphaned_tombstones(self) -> None:
        """Tombstone entries для несуществующих ID обнаруживаются."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id": "1", "ts": "2024-01-01T00:00:00", "text": "ok"}\n',
            encoding="utf-8",
        )
        tombstones_path = self.data_dir / "history_tombstones.ndjson"
        tombstones_path.write_text(
            '{"id": "999", "ts": "2024-01-01T00:00:00"}\n'
            '{"id": "1", "ts": "2024-01-02T00:00:00"}\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        self.assertGreater(report.orphaned_tombstones, 0)
        orphaned_check = next(
            (c for c in report.checks if c.name == "orphaned_tombstones"), None
        )
        self.assertIsNotNone(orphaned_check)
        self.assertTrue(orphaned_check.auto_fixable)

    def test_repair_removes_orphaned_tombstones(self) -> None:
        """repair() удаляет orphaned tombstone entries, сохраняя валидные."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id": "1", "ts": "2024-01-01T00:00:00", "text": "ok"}\n'
            '{"id": "2", "ts": "2024-01-02T00:00:00", "text": "ok"}\n',
            encoding="utf-8",
        )
        tombstones_path = self.data_dir / "history_tombstones.ndjson"
        tombstones_path.write_text(
            '{"id": "1", "ts": "2024-01-01T00:00:00"}\n'
            '{"id": "999", "ts": "2024-01-02T00:00:00"}\n'
            '{"id": "2", "ts": "2024-01-03T00:00:00"}\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertGreaterEqual(result.fixed, 0)
        remaining_tombstones = []
        for line in tombstones_path.read_text(encoding="utf-8").strip().splitlines():
            if line.strip():
                remaining_tombstones.append(json.loads(line))
        remaining_ids = {t.get("id") for t in remaining_tombstones}
        self.assertNotIn("999", remaining_ids)
        self.assertIn("1", remaining_ids)

    def test_check_integrity_with_corrupted_settings_json(self) -> None:
        """Повреждённый settings.json обнаруживается как ошибка."""
        settings_path = self.data_dir / "settings.json"
        settings_path.write_text('{"invalid json', encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        settings_check = next(
            (c for c in report.checks if c.name == "settings_json"), None
        )
        self.assertIsNotNone(settings_check)
        self.assertEqual(settings_check.status, "error")
        self.assertTrue(settings_check.auto_fixable)

    def test_repair_fixes_corrupted_settings_json(self) -> None:
        """repair() восстанавливает повреждённый settings.json до {}."""
        settings_path = self.data_dir / "settings.json"
        settings_path.write_text('{"broken json', encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertGreaterEqual(result.fixed, 0)
        repaired = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertIsInstance(repaired, dict)

    def test_check_integrity_timestamp_format_validation(self) -> None:
        """Items с неправильным форматом timestamp обнаруживаются."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id": "1", "ts": "2024-01-01T00:00:00", "text": "ok"}\n'
            '{"id": "2", "ts": "not a timestamp", "text": "ok"}\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        ts_check = next(
            (c for c in report.checks if c.name == "timestamp_format"), None
        )
        self.assertIsNotNone(ts_check)
        self.assertIn(ts_check.status, ("warning", "error"))

    def test_check_integrity_detects_duplicate_ids(self) -> None:
        """Items с дублирующимися ID обнаруживаются."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id": "same-id", "ts": "2024-01-01T00:00:00", "text": "first"}\n'
            '{"id": "same-id", "ts": "2024-01-02T00:00:00", "text": "duplicate"}\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        dup_check = next(
            (c for c in report.checks if c.name == "duplicate_ids"), None
        )
        self.assertIsNotNone(dup_check)
        self.assertEqual(dup_check.status, "warning")

    def test_ndjson_repair_creates_tmp_file_safely(self) -> None:
        """repair() использует .tmp файл для безопасной замены."""
        history_path = self.data_dir / "history.ndjson"
        original_content = (
            '{"id": "1", "ts": "2024-01-01T00:00:00", "text": "ok"}\n'
            'INVALID\n'
        )
        history_path.write_text(original_content, encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertIsInstance(result, RepairResult)
        self.assertTrue(history_path.exists())
        tmp_path = history_path.with_suffix(".ndjson.tmp")
        self.assertFalse(tmp_path.exists())


class IntegrityCheckerIPCTestCase(unittest.TestCase):
    """Проверяет IPC-хэндлеры check_integrity и repair_integrity."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")

        from unittest.mock import MagicMock
        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        self.svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )

    def test_check_integrity_handler(self) -> None:
        resp = self.svc.handle_request(
            {"id": "1", "method": "check_integrity", "params": {}}
        )
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("status", result)
        self.assertIn("checks", result)
        self.assertIn("total_items", result)

    def test_repair_integrity_handler(self) -> None:
        resp = self.svc.handle_request(
            {"id": "2", "method": "repair_integrity", "params": {}}
        )
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("fixed", result)
        self.assertIn("skipped", result)
        self.assertIn("details", result)


if __name__ == "__main__":
    unittest.main()
