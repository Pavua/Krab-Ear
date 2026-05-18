"""Дополнительные тесты для IntegrityChecker (Wave 87 extras).

Покрывает edge cases сверх test_integrity_checker.py:
- missing field (каждое из id/ts/text отдельно + null + empty string)
- type mismatch (bad ts format variations)
- orphan tombstone: single, multiple, all orphan
- duplicate ID: single pair, multiple pairs, 3× same id
- repair flow: dry-run vs actual (no file mutation on check)
- file-lock / concurrent access safety
- malformed JSON: partial JSON, array line, number line
- backup / atomic tmp → replace pattern
- IPC handler wiring without BackendService (direct checker)
- RepairResult details content
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.integrity_checker import (
    IntegrityChecker,
    IntegrityReport,
    RepairResult,
    REQUIRED_ITEM_FIELDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_ndjson(path: Path, items: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
        encoding="utf-8",
    )


def _write_tombstones(path: Path, tombstone_ids: list[str]) -> None:
    _write_ndjson(path, [{"id": tid, "deleted": True} for tid in tombstone_ids])


# ---------------------------------------------------------------------------
# Missing required fields (granular)
# ---------------------------------------------------------------------------

class TestMissingFields(unittest.TestCase):
    """Granular missing-field detection (id / ts / text separately)."""

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _rf_status(self) -> str:
        report = self.checker.check_integrity(self.data_dir)
        return next(c for c in report.checks if c.name == "required_fields").status

    def test_missing_id(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"ts": "2025-01-01T10:00:00", "text": "no id"},
        ])
        self.assertEqual(self._rf_status(), "error")

    def test_missing_ts(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "x1", "text": "no ts"},
        ])
        self.assertEqual(self._rf_status(), "error")

    def test_missing_text(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "x2", "ts": "2025-01-01T10:00:00"},
        ])
        self.assertEqual(self._rf_status(), "error")

    def test_null_id_is_missing(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": None, "ts": "2025-01-01T10:00:00", "text": "null id"},
        ])
        self.assertEqual(self._rf_status(), "error")

    def test_empty_string_id_is_missing(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "", "ts": "2025-01-01T10:00:00", "text": "empty id"},
        ])
        self.assertEqual(self._rf_status(), "error")

    def test_empty_string_text_is_missing(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "x3", "ts": "2025-01-01T10:00:00", "text": ""},
        ])
        self.assertEqual(self._rf_status(), "error")

    def test_all_fields_present_ok(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "ok1", "ts": "2025-05-01T10:00:00", "text": "all good"},
        ])
        self.assertEqual(self._rf_status(), "ok")

    def test_partial_missing_only_bad_items_flagged(self) -> None:
        """Один ok + один с missing → error (counted items = 1)."""
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "ok1", "ts": "2025-01-01T10:00:00", "text": "ok"},
            {"id": "bad", "ts": "2025-01-02T10:00:00"},  # no text
        ])
        report = self.checker.check_integrity(self.data_dir)
        rf = next(c for c in report.checks if c.name == "required_fields")
        self.assertEqual(rf.status, "error")
        # Only the bad item counted
        self.assertIn("1", rf.message)

    def test_required_fields_constant_has_id_ts_text(self) -> None:
        for field in ("id", "ts", "text"):
            self.assertIn(field, REQUIRED_ITEM_FIELDS)


# ---------------------------------------------------------------------------
# Timestamp format (type mismatch)
# ---------------------------------------------------------------------------

class TestTimestampFormatMismatch(unittest.TestCase):
    """Type mismatch: bad ts format in various forms."""

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _ts_status(self) -> str:
        report = self.checker.check_integrity(self.data_dir)
        return next(c for c in report.checks if c.name == "timestamp_format").status

    def test_dd_mm_yyyy_format_warning(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "t1", "ts": "01/01/2025", "text": "bad ts"},
        ])
        self.assertEqual(self._ts_status(), "warning")

    def test_plain_date_no_time_warning(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "t2", "ts": "2025-01-01", "text": "date only"},
        ])
        self.assertEqual(self._ts_status(), "warning")

    def test_freeform_string_warning(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "t3", "ts": "yesterday", "text": "freeform"},
        ])
        self.assertEqual(self._ts_status(), "warning")

    def test_valid_iso_ok(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "t4", "ts": "2025-06-15T12:30:45", "text": "ok"},
        ])
        self.assertEqual(self._ts_status(), "ok")

    def test_valid_iso_with_tz_ok(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "t5", "ts": "2025-06-15T12:30:45+00:00", "text": "ok tz"},
        ])
        self.assertEqual(self._ts_status(), "ok")

    def test_mixed_valid_invalid_warning(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "t6", "ts": "2025-06-15T12:30:45", "text": "ok"},
            {"id": "t7", "ts": "Jan 1 2025", "text": "bad"},
        ])
        self.assertEqual(self._ts_status(), "warning")


# ---------------------------------------------------------------------------
# Orphan tombstones
# ---------------------------------------------------------------------------

class TestOrphanTombstoneGranular(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_single_orphan(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        _write_tombstones(self.data_dir / "history_tombstones.ndjson", ["ghost"])
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.orphaned_tombstones, 1)

    def test_all_tombstones_orphaned(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        _write_tombstones(self.data_dir / "history_tombstones.ndjson",
                          ["ghost1", "ghost2", "ghost3"])
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.orphaned_tombstones, 3)

    def test_tombstone_for_existing_id_not_orphan(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        _write_tombstones(self.data_dir / "history_tombstones.ndjson", ["real"])
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.orphaned_tombstones, 0)

    def test_mixed_orphan_and_valid_tombstones(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "a", "ts": "2025-01-01T10:00:00", "text": "x"},
            {"id": "b", "ts": "2025-01-02T10:00:00", "text": "y"},
        ])
        _write_tombstones(self.data_dir / "history_tombstones.ndjson",
                          ["a", "ghost1", "ghost2"])  # 1 valid, 2 orphans
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.orphaned_tombstones, 2)

    def test_repair_orphan_tombstones_leaves_valid(self) -> None:
        """After repair, valid tombstones remain, orphans are removed."""
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        ts_path = self.data_dir / "history_tombstones.ndjson"
        _write_tombstones(ts_path, ["real", "ghost1", "ghost2"])
        report = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report)

        remaining = [
            json.loads(line)
            for line in ts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        remaining_ids = {t.get("id") for t in remaining}
        self.assertIn("real", remaining_ids)
        self.assertNotIn("ghost1", remaining_ids)
        self.assertNotIn("ghost2", remaining_ids)

    def test_orphan_tombstone_is_auto_fixable(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        _write_tombstones(self.data_dir / "history_tombstones.ndjson", ["ghost"])
        report = self.checker.check_integrity(self.data_dir)
        ot = next(c for c in report.checks if c.name == "orphaned_tombstones")
        self.assertTrue(ot.auto_fixable)


# ---------------------------------------------------------------------------
# Duplicate IDs
# ---------------------------------------------------------------------------

class TestDuplicateIDsGranular(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _dup_check(self) -> object:
        report = self.checker.check_integrity(self.data_dir)
        return next(c for c in report.checks if c.name == "duplicate_ids")

    def test_single_duplicate_pair(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "d1", "ts": "2025-01-01T10:00:00", "text": "a"},
            {"id": "d1", "ts": "2025-01-02T10:00:00", "text": "b"},
        ])
        self.assertEqual(self._dup_check().status, "warning")

    def test_triple_same_id(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "dup", "ts": "2025-01-01T10:00:00", "text": "a"},
            {"id": "dup", "ts": "2025-01-02T10:00:00", "text": "b"},
            {"id": "dup", "ts": "2025-01-03T10:00:00", "text": "c"},
        ])
        self.assertEqual(self._dup_check().status, "warning")

    def test_multiple_different_duplicate_pairs(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "p1", "ts": "2025-01-01T10:00:00", "text": "a"},
            {"id": "p1", "ts": "2025-01-02T10:00:00", "text": "b"},
            {"id": "p2", "ts": "2025-01-03T10:00:00", "text": "c"},
            {"id": "p2", "ts": "2025-01-04T10:00:00", "text": "d"},
        ])
        check = self._dup_check()
        self.assertEqual(check.status, "warning")
        # 2 дублирующихся ID в сообщении
        self.assertIn("2", check.message)

    def test_no_duplicates_ok(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "u1", "ts": "2025-01-01T10:00:00", "text": "a"},
            {"id": "u2", "ts": "2025-01-02T10:00:00", "text": "b"},
        ])
        self.assertEqual(self._dup_check().status, "ok")

    def test_duplicate_not_auto_fixable(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "d1", "ts": "2025-01-01T10:00:00", "text": "a"},
            {"id": "d1", "ts": "2025-01-02T10:00:00", "text": "b"},
        ])
        check = self._dup_check()
        self.assertFalse(check.auto_fixable)


# ---------------------------------------------------------------------------
# Malformed JSON granular
# ---------------------------------------------------------------------------

class TestMalformedJSONGranular(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_partial_json_detected(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text('{"id":"1","ts":"2025-01-01T10:00:00","text":"ok"}\n{broken\n',
                        encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 1)

    def test_json_array_line_is_bad(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text('[1,2,3]\n', encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 1)

    def test_json_number_line_is_bad(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text('42\n', encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 1)

    def test_json_string_line_is_bad(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text('"just a string"\n', encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 1)

    def test_valid_ndjson_auto_fixable(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text('BADLINE\n', encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        vj = next(c for c in report.checks if c.name == "valid_ndjson")
        self.assertTrue(vj.auto_fixable)

    def test_repair_malformed_json_result_details(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBAD\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertTrue(any("valid_ndjson" in d for d in result.details))


# ---------------------------------------------------------------------------
# Repair dry-run: check does NOT modify files
# ---------------------------------------------------------------------------

class TestDryRunCheck(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_check_does_not_modify_history(self) -> None:
        path = self.data_dir / "history.ndjson"
        content = '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBADLINE\n'
        path.write_text(content, encoding="utf-8")
        mtime = path.stat().st_mtime

        self.checker.check_integrity(self.data_dir)

        self.assertEqual(path.stat().st_mtime, mtime)
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_check_does_not_modify_tombstones(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        ts_path = self.data_dir / "history_tombstones.ndjson"
        _write_tombstones(ts_path, ["ghost"])
        ts_content = ts_path.read_text(encoding="utf-8")
        mtime = ts_path.stat().st_mtime

        self.checker.check_integrity(self.data_dir)

        self.assertEqual(ts_path.stat().st_mtime, mtime)
        self.assertEqual(ts_path.read_text(encoding="utf-8"), ts_content)

    def test_repair_after_check_returns_repair_result(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBAD\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        self.assertIsInstance(result, RepairResult)
        self.assertGreater(result.fixed, 0)


# ---------------------------------------------------------------------------
# Backup / atomic tmp→replace
# ---------------------------------------------------------------------------

class TestAtomicBackup(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_no_tmp_files_after_repair_ndjson(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBAD\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report)

        tmp_files = list(self.data_dir.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_no_tmp_files_after_repair_tombstones(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        _write_tombstones(self.data_dir / "history_tombstones.ndjson", ["ghost"])
        report = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report)

        tmp_files = list(self.data_dir.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_no_tmp_files_after_repair_settings(self) -> None:
        (self.data_dir / "settings.json").write_text("BAD JSON", encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report)

        tmp_files = list(self.data_dir.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_repaired_file_is_valid_after_partial_corruption(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text(
            '{"id":"a","ts":"2025-01-01T10:00:00","text":"ok"}\n'
            '{bad json\n'
            '{"id":"b","ts":"2025-01-02T10:00:00","text":"ok2"}\n',
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report)

        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(lines), 2)
        for line in lines:
            obj = json.loads(line)
            self.assertIsInstance(obj, dict)


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------

class TestConcurrentAccess(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_concurrent_reads_no_exception(self) -> None:
        """Multiple simultaneous check_integrity calls do not raise."""
        items = [
            {"id": f"item{i}", "ts": f"2025-01-{i+1:02d}T10:00:00", "text": f"t{i}"}
            for i in range(10)
        ]
        _write_ndjson(self.data_dir / "history.ndjson", items)

        errors: list[Exception] = []

        def run():
            try:
                self.checker.check_integrity(self.data_dir)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])

    def test_repair_followed_by_check_is_clean(self) -> None:
        """After repair, a second check shows no errors for the repaired issue."""
        path = self.data_dir / "history.ndjson"
        path.write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBAD\n',
            encoding="utf-8",
        )

        report1 = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report1)

        report2 = self.checker.check_integrity(self.data_dir)
        vj = next(c for c in report2.checks if c.name == "valid_ndjson")
        self.assertEqual(vj.status, "ok")


# ---------------------------------------------------------------------------
# IPC handler wiring (direct checker, no BackendService)
# ---------------------------------------------------------------------------

class TestIPCHandlersDirect(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "i1", "ts": "2025-01-01T10:00:00", "text": "hi"},
        ])

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_handle_check_integrity_structure(self) -> None:
        result = self.checker.handle_check_integrity({"data_dir": str(self.data_dir)})

        for key in ("status", "total_items", "checks", "orphaned_tombstones",
                    "invalid_json_lines"):
            self.assertIn(key, result)

    def test_handle_check_integrity_each_check_has_required_fields(self) -> None:
        result = self.checker.handle_check_integrity({"data_dir": str(self.data_dir)})
        for check in result["checks"]:
            for key in ("name", "status", "message", "auto_fixable"):
                self.assertIn(key, check)
            self.assertIn(check["status"], ("ok", "warning", "error"))

    def test_handle_check_integrity_missing_param_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.checker.handle_check_integrity({})

    def test_handle_repair_data_structure(self) -> None:
        result = self.checker.handle_repair_data({"data_dir": str(self.data_dir)})
        for key in ("fixed", "skipped", "details"):
            self.assertIn(key, result)
        self.assertIsInstance(result["details"], list)

    def test_handle_repair_data_missing_param_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.checker.handle_repair_data({})

    def test_handle_repair_data_with_bad_ndjson(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBAD_LINE\n',
            encoding="utf-8",
        )
        result = self.checker.handle_repair_data({"data_dir": str(self.data_dir)})
        self.assertGreater(result["fixed"], 0)


# ---------------------------------------------------------------------------
# RepairResult details
# ---------------------------------------------------------------------------

class TestRepairResultDetails(unittest.TestCase):

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_details_contain_valid_ndjson_entry(self) -> None:
        path = self.data_dir / "history.ndjson"
        path.write_text('BAD\n', encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        combined = "\n".join(result.details)
        self.assertIn("valid_ndjson", combined)

    def test_details_contain_orphaned_tombstones_entry(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        _write_tombstones(self.data_dir / "history_tombstones.ndjson", ["ghost"])
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        combined = "\n".join(result.details)
        self.assertIn("orphaned_tombstones", combined)

    def test_details_contain_settings_json_entry(self) -> None:
        (self.data_dir / "settings.json").write_text("BAD", encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        combined = "\n".join(result.details)
        self.assertIn("settings_json", combined)

    def test_skipped_non_fixable_issue(self) -> None:
        """Duplicate IDs are not auto_fixable → counted in skipped."""
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "dup", "ts": "2025-01-01T10:00:00", "text": "a"},
            {"id": "dup", "ts": "2025-01-02T10:00:00", "text": "b"},
        ])
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertGreaterEqual(result.skipped, 1)

    def test_clean_store_zero_fixed_zero_details(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "ok", "ts": "2025-06-01T10:00:00", "text": "clean"},
        ])
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)
        self.assertEqual(result.fixed, 0)
        self.assertEqual(result.details, [])


if __name__ == "__main__":
    unittest.main()
