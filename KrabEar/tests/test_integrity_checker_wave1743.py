"""Wave 1743 — data-loss safety tests for IntegrityChecker.

Covers:
- HIGH-1: backup exists + quarantine contains bad lines + repaired file has only valid lines
- HIGH-2: TOCTOU/lock safety (repair acquires flock on history.lock)
- MED regression: Cyrillic line is NOT corrupted/dropped
- Idempotent: running repair twice does not double-backup-churn or lose data
- Typo check: detail message no longer contains extra '}'
- backup_paths / quarantine_paths returned in RepairResult
- handle_repair_data exposes backup_paths / quarantine_paths
"""

from __future__ import annotations

import fcntl
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.integrity_checker import IntegrityChecker


def _write_ndjson(path: Path, items: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
        encoding="utf-8",
    )


class TestBackupAndQuarantineOnRepair(unittest.TestCase):
    """HIGH-1: backup + quarantine before ANY destructive write."""

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_corrupted_line_backup_exists_after_repair(self) -> None:
        """After repair of bad NDJSON, a backup file exists with ORIGINAL content."""
        history_path = self.data_dir / "history.ndjson"
        original = (
            '{"id":"1","ts":"2025-01-01T10:00:00","text":"good"}\n'
            'NOT VALID JSON\n'
            '{"id":"2","ts":"2025-01-02T10:00:00","text":"also good"}\n'
        )
        history_path.write_text(original, encoding="utf-8")

        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        # At least one backup path was created.
        self.assertTrue(
            result.backup_paths,
            "repair() must create at least one backup file",
        )
        backup_path = Path(result.backup_paths[0])
        self.assertTrue(backup_path.exists(), f"Backup file must exist: {backup_path}")
        # Backup contains the ORIGINAL content.
        self.assertEqual(backup_path.read_text(encoding="utf-8"), original)

    def test_corrupted_line_quarantine_contains_bad_line(self) -> None:
        """After repair, the bad line is in a quarantine file — NOT vanished."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"clean"}\n'
            'THIS IS BAD\n',
            encoding="utf-8",
        )

        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        self.assertTrue(result.quarantine_paths, "A quarantine file must be created")
        q_path = Path(result.quarantine_paths[0])
        self.assertTrue(q_path.exists(), f"Quarantine file must exist: {q_path}")
        q_content = q_path.read_bytes()
        self.assertIn(b"THIS IS BAD", q_content)

    def test_repaired_file_has_only_valid_lines(self) -> None:
        """After repair, the repaired file contains only the valid lines."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id":"a","ts":"2025-01-01T10:00:00","text":"ok1"}\n'
            'GARBAGE\n'
            '{"id":"b","ts":"2025-01-02T10:00:00","text":"ok2"}\n',
            encoding="utf-8",
        )

        report = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report)

        lines = [
            line for line in history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(lines), 2)
        for line in lines:
            obj = json.loads(line)
            self.assertIn(obj["id"], ("a", "b"))

    def test_no_backup_when_nothing_to_fix(self) -> None:
        """On a clean store, no backup files are created."""
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "ok", "ts": "2025-01-01T10:00:00", "text": "clean"},
        ])
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        self.assertEqual(result.backup_paths, [])
        self.assertEqual(result.quarantine_paths, [])

    def test_backup_path_returned_in_repair_result(self) -> None:
        """repair() returns backup_paths in RepairResult."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text("BAD LINE\n", encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        self.assertIsInstance(result.backup_paths, list)
        self.assertTrue(len(result.backup_paths) >= 1)

    def test_quarantine_path_returned_in_repair_result(self) -> None:
        """repair() returns quarantine_paths in RepairResult."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text("BAD LINE\n", encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        self.assertIsInstance(result.quarantine_paths, list)
        self.assertTrue(len(result.quarantine_paths) >= 1)

    def test_settings_backup_created_on_corrupt_settings(self) -> None:
        """repair() creates a backup of corrupted settings.json."""
        settings_path = self.data_dir / "settings.json"
        original_content = b'{"broken json'
        settings_path.write_bytes(original_content)

        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        self.assertTrue(result.backup_paths, "Backup must be created for corrupted settings")
        backup = Path(result.backup_paths[0])
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), original_content)

    def test_tombstones_not_modified_by_repair(self) -> None:
        """W1776 fix: repair() must not touch the tombstones file at all.

        The old check incorrectly treated tombstones for deleted items as
        'orphaned' and created backup/quarantine artefacts before erasing them.
        After the fix the check is disabled: repair() produces no backup_paths
        and the tombstones file is left intact.
        """
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        ts_path = self.data_dir / "history_tombstones.ndjson"
        _write_ndjson(ts_path, [{"id": "ghost", "deleted": True}])
        original = ts_path.read_bytes()

        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.orphaned_tombstones, 0)
        result = self.checker.repair(self.data_dir, report)

        # No backup should be created for tombstones (the check is disabled).
        tombstone_backups = [p for p in result.backup_paths
                             if "tombstone" in p]
        self.assertEqual(tombstone_backups, [])
        # Tombstones file must be unchanged.
        self.assertEqual(ts_path.read_bytes(), original)

    def test_tombstones_for_deleted_items_preserved_after_repair(self) -> None:
        """W1776 fix: tombstone entries for deleted items survive repair() intact."""
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "real", "ts": "2025-01-01T10:00:00", "text": "x"},
        ])
        _write_ndjson(self.data_dir / "history_tombstones.ndjson", [
            {"id": "ghost_id", "deleted": True},
        ])

        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        # No quarantine paths should be created (no tombstones are removed).
        tombstone_quarantines = [p for p in result.quarantine_paths
                                 if "tombstone" in p]
        self.assertEqual(tombstone_quarantines, [])
        # ghost_id tombstone must still be present.
        ts_content = (self.data_dir / "history_tombstones.ndjson").read_text(encoding="utf-8")
        self.assertIn("ghost_id", ts_content)


class TestCyrillicNotCorrupted(unittest.TestCase):
    """MED regression: errors='replace' was silently corrupting Cyrillic lines."""

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_cyrillic_valid_line_not_dropped(self) -> None:
        """A valid JSON line with Cyrillic text is kept after repair (not dropped or mojibaked)."""
        cyrillic_text = "Привет, мир! Это тестовая запись."
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            json.dumps(
                {"id": "ru1", "ts": "2025-05-01T10:00:00", "text": cyrillic_text},
                ensure_ascii=False,
            ) + "\n"
            + "BAD LINE\n",
            encoding="utf-8",
        )

        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 1)  # Only the BAD LINE

        self.checker.repair(self.data_dir, report)

        content = history_path.read_text(encoding="utf-8")
        lines = [ln for ln in content.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        recovered = json.loads(lines[0])
        self.assertEqual(recovered["text"], cyrillic_text)  # Exact match — no mojibake

    def test_cyrillic_line_check_not_flagged_as_invalid(self) -> None:
        """A well-formed Cyrillic JSON line is not counted as invalid."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            json.dumps(
                {"id": "ru2", "ts": "2025-05-01T10:00:00", "text": "Проверка целостности"},
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 0)
        self.assertEqual(report.total_items, 1)

    def test_spanish_and_mixed_unicode_valid(self) -> None:
        """Spanish + emoji in a valid JSON line passes without corruption."""
        mixed_text = "¡Hola mundo! 🎤 Transcripción"
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            json.dumps(
                {"id": "es1", "ts": "2025-05-01T10:00:00", "text": mixed_text},
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        report = self.checker.check_integrity(self.data_dir)
        self.assertEqual(report.invalid_json_lines, 0)

        # After a clean repair (nothing to fix), the line is unchanged
        result = self.checker.repair(self.data_dir, report)
        self.assertEqual(result.fixed, 0)
        content = history_path.read_text(encoding="utf-8")
        self.assertIn(mixed_text, content)


class TestLockAcquiredDuringRepair(unittest.TestCase):
    """HIGH-2: repair must hold the StateStore flock during read-modify-write."""

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_repair_acquires_lock_blocking_concurrent_writer(self) -> None:
        """While repair holds the lock, a concurrent writer cannot acquire it.

        We pre-acquire the lock in the main thread, start repair in a background
        thread, and verify that repair is blocked until we release the lock.
        """
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBAD\n',
            encoding="utf-8",
        )

        lock_path = self.data_dir / "history.lock"
        lock_path.touch(exist_ok=True)

        # Pre-acquire the exclusive lock from the main thread.
        lock_fd = lock_path.open("r+", encoding="utf-8")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)

        repair_started = threading.Event()
        repair_done = threading.Event()

        def _do_repair():
            repair_started.set()
            report = self.checker.check_integrity(self.data_dir)
            self.checker.repair(self.data_dir, report)
            repair_done.set()

        t = threading.Thread(target=_do_repair, daemon=True)
        t.start()
        repair_started.wait(timeout=2.0)

        # Repair should still be waiting for the lock.
        # Give it a moment to try to acquire.
        repair_done_before_release = not repair_done.wait(timeout=0.3)

        # Release the lock.
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()

        # Repair should now complete.
        repair_done.wait(timeout=5.0)

        self.assertTrue(
            repair_done_before_release,
            "repair() should have been blocked until we released the lock",
        )
        self.assertTrue(repair_done.is_set(), "repair() should complete after lock release")

    def test_repair_history_lock_file_used(self) -> None:
        """repair() uses history.lock, the same lock file as StateStore."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text("BAD LINE\n", encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        self.checker.repair(self.data_dir, report)

        # history.lock must exist after repair (created by _acquire_lock).
        lock_path = self.data_dir / "history.lock"
        self.assertTrue(lock_path.exists(), "history.lock must exist after repair")


class TestIdempotentRepair(unittest.TestCase):
    """Running repair twice must not double-backup-churn or lose data."""

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_double_repair_no_data_loss(self) -> None:
        """Second repair on an already-repaired file does nothing more."""
        history_path = self.data_dir / "history.ndjson"
        history_path.write_text(
            '{"id":"1","ts":"2025-01-01T10:00:00","text":"ok"}\nBAD\n',
            encoding="utf-8",
        )

        # First repair
        report1 = self.checker.check_integrity(self.data_dir)
        result1 = self.checker.repair(self.data_dir, report1)
        self.assertGreater(result1.fixed, 0)
        backups_after_first = len(result1.backup_paths)

        # Second repair — nothing to fix
        report2 = self.checker.check_integrity(self.data_dir)
        result2 = self.checker.repair(self.data_dir, report2)
        self.assertEqual(result2.fixed, 0)
        # No new backups created on a clean store
        self.assertEqual(result2.backup_paths, [])

        # Final file should still have the one valid line
        lines = [
            ln for ln in history_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], "1")

        # First repair must have created exactly 1 backup
        self.assertEqual(backups_after_first, 1)

    def test_idempotent_clean_store_no_backup_files(self) -> None:
        """On a clean store, zero backup/quarantine files are created after two passes."""
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "ok", "ts": "2025-01-01T10:00:00", "text": "clean"},
        ])
        for _ in range(2):
            report = self.checker.check_integrity(self.data_dir)
            result = self.checker.repair(self.data_dir, report)
            self.assertEqual(result.backup_paths, [])
            self.assertEqual(result.quarantine_paths, [])

        backup_files = list(self.data_dir.glob("*corrupt-backup*"))
        self.assertEqual(backup_files, [])


class TestDetailMessageNoExtraBrace(unittest.TestCase):
    """MED typo: detail message must NOT contain '}' (was '{}}'). """

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_settings_detail_no_extra_brace(self) -> None:
        (self.data_dir / "settings.json").write_text("BAD JSON", encoding="utf-8")
        report = self.checker.check_integrity(self.data_dir)
        result = self.checker.repair(self.data_dir, report)

        settings_detail = next(
            (d for d in result.details if "settings_json" in d), None
        )
        self.assertIsNotNone(settings_detail, "settings_json detail must exist")
        # Must end with '{}'  (no trailing extra brace)
        self.assertTrue(
            settings_detail.rstrip().endswith("{}"),
            f"Detail must end with '{{}}', got: {settings_detail!r}",
        )
        self.assertNotIn("{}}", settings_detail)


class TestHandleRepairDataExposesBackupPaths(unittest.TestCase):
    """handle_repair_data IPC handler exposes backup_paths and quarantine_paths."""

    def setUp(self) -> None:
        self.checker = IntegrityChecker()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_handle_repair_data_has_backup_and_quarantine_keys(self) -> None:
        (self.data_dir / "history.ndjson").write_text("BAD\n", encoding="utf-8")
        result = self.checker.handle_repair_data({"data_dir": str(self.data_dir)})
        self.assertIn("backup_paths", result)
        self.assertIn("quarantine_paths", result)

    def test_handle_repair_data_backup_paths_populated_when_bad(self) -> None:
        (self.data_dir / "history.ndjson").write_text(
            '{"id":"ok","ts":"2025-01-01T10:00:00","text":"x"}\nBAD\n',
            encoding="utf-8",
        )
        result = self.checker.handle_repair_data({"data_dir": str(self.data_dir)})
        self.assertTrue(result["backup_paths"])
        self.assertTrue(result["quarantine_paths"])

    def test_handle_repair_data_empty_paths_on_clean_store(self) -> None:
        _write_ndjson(self.data_dir / "history.ndjson", [
            {"id": "ok", "ts": "2025-01-01T10:00:00", "text": "clean"},
        ])
        result = self.checker.handle_repair_data({"data_dir": str(self.data_dir)})
        self.assertEqual(result["backup_paths"], [])
        self.assertEqual(result["quarantine_paths"], [])


if __name__ == "__main__":
    unittest.main()
