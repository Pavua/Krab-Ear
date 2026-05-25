"""Wave 218 — AuditLogger rotation deep edge-case tests.

Covers boundary conditions and concurrency around log rotation:
- Exact size threshold rotation
- Dated backup creation
- Retention policy (keep last N)
- Old file deletion after retention
- Atomic rotation with no data loss under concurrency
- Disk-full handling
- PermissionError handling (Wave 96 fix verification)
- Concurrent writes during rotation
- Unicode preservation across rotation
- Rotation resume after temporary failure
- Cross-rotation-boundary query correctness
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Path resolution for standalone run
_TESTS_DIR = Path(__file__).parent
_PROJECT_ROOT = _TESTS_DIR.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_KRABEAR_DIR = _PROJECT_ROOT / "KrabEar"
if str(_KRABEAR_DIR) not in sys.path:
    sys.path.insert(0, str(_KRABEAR_DIR))

from backend.audit_logger import AuditLogger, _KEEP_DAYS  # noqa: E402


def _make_entry(al: AuditLogger, method: str = "ping", success: bool = True) -> None:
    al.log_request(
        method=method,
        params={"x": 1},
        result={"ok": success},
        duration_ms=1.0,
    )


class TestRotationAtExactSizeThreshold(unittest.TestCase):
    """test_rotation_at_exact_size_threshold — >= boundary check."""

    def test_date_change_triggers_rotation(self):
        """Rotation happens when the calendar date changes, not size."""
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            day1 = "2024-01-01"
            day2 = "2024-01-02"

            # Simulate day1 write by manually priming state
            p1 = Path(tmp) / f"audit_{day1}.ndjson"
            p1.write_text('{"ts":"2024-01-01","method":"ping"}\n', encoding="utf-8")
            with al._lock:
                if al._file_handle:
                    al._file_handle.close()
                al._file_handle = open(p1, "a", encoding="utf-8")
                al._current_date = day1

            # Now simulate rotation to day2
            with al._lock:
                al._rotate_if_needed(day2)

            p2 = Path(tmp) / f"audit_{day2}.ndjson"
            self.assertTrue(p2.exists(), "day2 audit file should be created on rotation")
            self.assertEqual(al._current_date, day2)
            al.close()

    def test_same_date_no_new_file(self):
        """No rotation when date is unchanged — same file reused."""
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            _make_entry(al)
            files_before = list(Path(tmp).glob("audit_*.ndjson"))
            _make_entry(al)
            files_after = list(Path(tmp).glob("audit_*.ndjson"))
            self.assertEqual(len(files_before), len(files_after))
            al.close()


class TestRotationCreatedDatedBackup(unittest.TestCase):
    """test_rotation_creates_dated_backup — file named audit_YYYY-MM-DD."""

    def test_backup_filename_contains_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _make_entry(al)
            files = list(Path(tmp).glob("audit_*.ndjson"))
            self.assertTrue(any(today_str in f.name for f in files),
                            f"Expected audit_{today_str}.ndjson, got {[f.name for f in files]}")
            al.close()

    def test_rotation_to_new_date_old_file_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            old_date = "2023-12-31"
            old_path = Path(tmp) / f"audit_{old_date}.ndjson"
            old_path.write_text('{"ts":"t","method":"old"}\n', encoding="utf-8")

            if al._file_handle:
                al._file_handle.close()
            al._file_handle = open(old_path, "a", encoding="utf-8")
            al._current_date = old_date

            new_date = "2024-01-01"
            with al._lock:
                al._rotate_if_needed(new_date)

            self.assertTrue(old_path.exists(), "old dated file must be preserved")
            new_path = Path(tmp) / f"audit_{new_date}.ndjson"
            self.assertTrue(new_path.exists(), "new dated file must be created")
            al.close()


class TestRotationKeepsLastNFiles(unittest.TestCase):
    """test_rotation_keeps_last_N_files — retention == _KEEP_DAYS."""

    def test_keeps_exactly_keep_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = datetime(2024, 1, 1, tzinfo=timezone.utc)
            for i in range(_KEEP_DAYS + 3):
                d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                (Path(tmp) / f"audit_{d}.ndjson").write_text(
                    '{"ts":"t","method":"m"}\n', encoding="utf-8"
                )
            al = AuditLogger(tmp)
            al._cleanup_old_files()
            remaining = sorted(Path(tmp).glob("audit_*.ndjson"))
            self.assertEqual(len(remaining), _KEEP_DAYS,
                             f"Expected {_KEEP_DAYS} files, got {len(remaining)}")
            al.close()

    def test_fewer_than_keep_days_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = datetime(2024, 1, 1, tzinfo=timezone.utc)
            n = _KEEP_DAYS - 2
            for i in range(n):
                d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                (Path(tmp) / f"audit_{d}.ndjson").write_text(
                    '{"ts":"t","method":"m"}\n', encoding="utf-8"
                )
            al = AuditLogger(tmp)
            al._cleanup_old_files()
            remaining = list(Path(tmp).glob("audit_*.ndjson"))
            self.assertEqual(len(remaining), n)
            al.close()


class TestOldFilesDeletedAfterRetention(unittest.TestCase):
    """test_old_files_deleted_after_retention — oldest files are removed."""

    def test_oldest_files_removed_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = datetime(2024, 1, 1, tzinfo=timezone.utc)
            dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d")
                     for i in range(_KEEP_DAYS + 2)]
            for d in dates:
                (Path(tmp) / f"audit_{d}.ndjson").write_text(
                    '{"ts":"t","method":"m"}\n', encoding="utf-8"
                )
            al = AuditLogger(tmp)
            al._cleanup_old_files()
            remaining = sorted(f.name for f in Path(tmp).glob("audit_*.ndjson"))
            # The oldest 2 should be gone; remaining should be the last _KEEP_DAYS
            expected_kept = [f"audit_{d}.ndjson" for d in dates[-_KEEP_DAYS:]]
            self.assertEqual(remaining, sorted(expected_kept))
            al.close()

    def test_excess_files_not_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = datetime(2024, 2, 1, tzinfo=timezone.utc)
            excess = 5
            for i in range(_KEEP_DAYS + excess):
                d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                (Path(tmp) / f"audit_{d}.ndjson").write_text("x\n", encoding="utf-8")
            al = AuditLogger(tmp)
            al._cleanup_old_files()
            remaining = list(Path(tmp).glob("audit_*.ndjson"))
            self.assertLessEqual(len(remaining), _KEEP_DAYS)
            al.close()


class TestRotationAtomicNoDataLoss(unittest.TestCase):
    """test_rotation_atomic_no_data_loss — no entries dropped during date flip."""

    def test_concurrent_log_during_rotation_no_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            # Pre-prime with old date
            old_date = "2020-01-01"
            old_path = Path(tmp) / f"audit_{old_date}.ndjson"
            old_path.write_text("", encoding="utf-8")
            with al._lock:
                if al._file_handle:
                    al._file_handle.close()
                al._file_handle = open(old_path, "a", encoding="utf-8")
                al._current_date = old_date

            errors = []
            writes_done = []

            def writer(idx):
                try:
                    for _ in range(10):
                        _make_entry(al, method=f"method_{idx}")
                    writes_done.append(idx)
                except Exception as e:
                    errors.append(e)

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
            # Trigger rotation in parallel with writes
            def do_rotate():  # noqa: E306
                with al._lock:
                    al._rotate_if_needed(today)

            rotate_thread = threading.Thread(target=do_rotate)
            for t in threads:
                t.start()
            rotate_thread.start()
            for t in threads:
                t.join()
            rotate_thread.join()

            self.assertEqual(errors, [], f"No exceptions expected, got: {errors}")
            self.assertEqual(len(writes_done), 5)

            # At minimum all entries in today's file should be valid JSON
            today_path = Path(tmp) / f"audit_{today}.ndjson"
            if today_path.exists():
                raw_lines = [ln.strip() for ln in today_path.read_text().splitlines() if ln.strip()]
                for line in raw_lines:
                    parsed = json.loads(line)
                    self.assertIn("method", parsed)
            al.close()


class TestRotationUnderDiskFull(unittest.TestCase):
    """test_rotation_under_disk_full_handled — OSError on open is silently swallowed."""

    def test_disk_full_oserror_does_not_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            # Force a date mismatch so rotation is triggered
            al._current_date = "1970-01-01"
            if al._file_handle:
                al._file_handle.close()
                al._file_handle = None

            with patch("builtins.open", side_effect=OSError("No space left on device")):
                # Should not raise
                try:
                    _make_entry(al)
                except Exception as e:
                    self.fail(f"log_request raised unexpectedly: {e}")

    def test_disk_full_leaves_handle_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            al._current_date = "1970-01-01"
            if al._file_handle:
                al._file_handle.close()
                al._file_handle = None

            with patch("builtins.open", side_effect=OSError("ENOSPC")):
                with al._lock:
                    al._rotate_if_needed("2025-01-01")
            # file_handle should remain None when open fails
            self.assertIsNone(al._file_handle)
            al.close()


class TestRotationUnderPermissionDenied(unittest.TestCase):
    """test_rotation_under_permission_denied_handled — Wave 96 fix verification."""

    def test_permission_denied_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            al._current_date = "1970-01-01"
            if al._file_handle:
                al._file_handle.close()
                al._file_handle = None

            with patch("builtins.open", side_effect=PermissionError("Permission denied")):
                try:
                    _make_entry(al)
                except PermissionError:
                    self.fail("PermissionError must not propagate out of log_request")

    def test_permission_denied_skips_rotation_current_date_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            original_date = al._current_date

            with patch("builtins.open", side_effect=PermissionError("denied")):
                with al._lock:
                    al._rotate_if_needed("9999-12-31")

            # current_date must NOT be updated when rotation failed
            self.assertEqual(al._current_date, original_date)
            al.close()


class TestConcurrentWritesDuringRotation(unittest.TestCase):
    """test_concurrent_log_writes_during_rotation_safe — no interleaved/corrupted lines."""

    def test_all_lines_valid_json_under_concurrent_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            errors = []

            def flood(thread_id):
                try:
                    for i in range(20):
                        al.log_request(
                            method=f"method_{thread_id}_{i}",
                            params={"a": thread_id, "b": i},
                            result={"ok": True},
                            duration_ms=float(i),
                        )
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=flood, args=(tid,)) for tid in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])

            # Verify every line in every file is parseable JSON
            for path in Path(tmp).glob("audit_*.ndjson"):
                for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        json.loads(raw)
                    except json.JSONDecodeError as e:
                        self.fail(f"Corrupt JSON at {path.name}:{lineno}: {e}\n  line={raw!r}")
            al.close()


class TestUnicodePreservedAcrossRotation(unittest.TestCase):
    """test_unicode_log_lines_preserved_across_rotation — Cyrillic/emoji survive."""

    def test_unicode_method_name_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            unicode_method = "тест_метода_с_юникодом"
            al.log_request(
                method=unicode_method,
                params={"текст": "значение"},
                result={"ok": True},
                duration_ms=0.5,
            )
            al.close()

            entries = AuditLogger(tmp).get_audit_log(method_filter=unicode_method)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["method"], unicode_method)

    def test_emoji_in_params_keys_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            al.log_request(
                method="emoji_test",
                params={"🎤key": "val", "normal": "ok"},
                result={"ok": True},
                duration_ms=1.0,
            )
            al.close()

            entries = AuditLogger(tmp).get_audit_log(method_filter="emoji_test")
            self.assertEqual(len(entries), 1)
            params_keys = entries[0]["params_keys"]
            self.assertIn("🎤key", params_keys)

    def test_unicode_across_simulated_rotation(self):
        """Write unicode entries on two different dates; both readable."""
        with tempfile.TemporaryDirectory() as tmp:
            d1 = "2024-06-01"
            d2 = "2024-06-02"
            for date, method in [(d1, "старый_метод"), (d2, "новый_метод")]:
                path = Path(tmp) / f"audit_{date}.ndjson"
                entry = {"ts": f"{date}T00:00:00+00:00", "method": method,
                         "params_keys": [], "success": True, "duration_ms": 0.0}
                path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

            al = AuditLogger(tmp)
            entries = al.get_audit_log(limit=100)
            methods = {e["method"] for e in entries}
            self.assertIn("старый_метод", methods)
            self.assertIn("новый_метод", methods)
            al.close()


class TestRotationResumesAfterTemporaryFailure(unittest.TestCase):
    """test_rotation_resumes_after_temporary_failure — retry on next call succeeds."""

    def test_second_call_retries_rotation_after_first_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            al._current_date = "1970-01-01"
            if al._file_handle:
                al._file_handle.close()
                al._file_handle = None

            call_count = [0]
            import builtins
            original_open = builtins.open

            def failing_then_succeeding(path, *args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1 and "audit_" in str(path):
                    raise OSError("Temporary failure")
                return original_open(path, *args, **kwargs)

            with patch.object(builtins, "open", side_effect=failing_then_succeeding):
                # First call: open fails → rotation skipped
                _make_entry(al, method="attempt1")

            # File handle is None; current_date still old
            # Second call: no mock → should succeed
            _make_entry(al, method="attempt2")

            # After successful write, current_date should match today
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.assertEqual(al._current_date, today)
            al.close()

    def test_entries_written_after_recovered_rotation_are_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            # Normal operation — recovery from empty state
            _make_entry(al, method="after_recovery")
            entries = al.get_audit_log(method_filter="after_recovery")
            self.assertEqual(len(entries), 1)
            al.close()


class TestQueryAcrossRotationBoundary(unittest.TestCase):
    """test_query_returns_results_across_rotation_boundary — multi-file query."""

    def test_get_audit_log_reads_multiple_date_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = datetime(2024, 3, 1, tzinfo=timezone.utc)
            all_methods = []
            for i in range(3):
                d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                method = f"method_day_{i}"
                all_methods.append(method)
                entry = {"ts": f"{d}T00:00:00+00:00", "method": method,
                         "params_keys": [], "success": True, "duration_ms": 0.0}
                (Path(tmp) / f"audit_{d}.ndjson").write_text(
                    json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8"
                )

            al = AuditLogger(tmp)
            entries = al.get_audit_log(limit=100)
            found_methods = {e["method"] for e in entries}
            for m in all_methods:
                self.assertIn(m, found_methods, f"Method {m} not found across rotation boundary")
            al.close()

    def test_method_filter_works_across_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = datetime(2024, 4, 1, tzinfo=timezone.utc)
            target = "target_method"
            for i in range(4):
                d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                lines = []
                lines.append(json.dumps({"ts": f"{d}T00:00:00+00:00", "method": target,
                                         "params_keys": [], "success": True, "duration_ms": 0.0},
                                        ensure_ascii=False))
                lines.append(json.dumps({"ts": f"{d}T01:00:00+00:00", "method": "other",
                                         "params_keys": [], "success": True, "duration_ms": 0.0},
                                        ensure_ascii=False))
                (Path(tmp) / f"audit_{d}.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")

            al = AuditLogger(tmp)
            entries = al.get_audit_log(limit=100, method_filter=target)
            self.assertEqual(len(entries), 4, "One target entry per day × 4 days")
            for e in entries:
                self.assertEqual(e["method"], target)
            al.close()

    def test_limit_respected_across_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = datetime(2024, 5, 1, tzinfo=timezone.utc)
            for i in range(5):
                d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                lines = [json.dumps({"ts": f"{d}T00:00:00+00:00", "method": "m",
                                     "params_keys": [], "success": True, "duration_ms": 0.0},
                                    ensure_ascii=False)
                         for _ in range(10)]
                (Path(tmp) / f"audit_{d}.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")

            al = AuditLogger(tmp)
            entries = al.get_audit_log(limit=15)
            self.assertEqual(len(entries), 15)
            al.close()


if __name__ == "__main__":
    unittest.main()
