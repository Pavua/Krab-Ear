"""RMW сохраняет exports при I/O-сбое и повторном здоровом чтении.

Доказанная corruption сохраняется move-aside с прежним retention=5;
read-only сбои не меняют исходный журнал или payload-файлы.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _plaintext_export_test_helpers import off_authorizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.export_scheduler import ExportScheduler  # noqa: E402


class ExportScheduleCorruptRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.sched = ExportScheduler(data_dir=self.dir, plaintext_export_authorizer=off_authorizer())
        self.path = self.dir / "export_schedule.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _copies(self) -> list[Path]:
        return sorted(self.dir.glob("export_schedule.json.corrupt-*"))

    def _healthy_fixture(self) -> bytes:
        raw = (b'{"enabled": true, "last_export_ts": "2026-01-01T12:00:00+00:00", '
               b'"exports": [{"path": "literal-existing-export"}]}')
        self.path.write_bytes(raw)
        (self.dir / "literal-existing-export").write_bytes(b"existing payload")
        return raw

    def _snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.dir)): p.read_bytes()
                for p in self.dir.rglob("*") if p.is_file()}

    def _mutation(self, operation: str) -> dict:
        if operation == "configure":
            return self.sched.configure(fmt="csv", interval_hours=6, enabled=True)
        return self.sched.cancel()

    def test_transient_read_error_blocks_configure(self) -> None:
        self._assert_read_error_blocks_mutation("configure", transient=True)

    def test_transient_read_error_blocks_cancel(self) -> None:
        self._assert_read_error_blocks_mutation("cancel", transient=True)

    def test_persistent_read_error_blocks_mutations(self) -> None:
        for operation in ("configure", "cancel"):
            self._assert_read_error_blocks_mutation(operation, transient=False)

    def _assert_read_error_blocks_mutation(self, operation: str, *, transient: bool) -> None:
        for error_type in (PermissionError, OSError):
            with self.subTest(operation=operation, error_type=error_type, transient=transient):
                raw = self._healthy_fixture()
                before = self._snapshot()
                read = self.sched._read_schedule_file
                calls = 0

                def read_fails():
                    nonlocal calls
                    calls += 1
                    if calls == 1 or not transient:
                        raise error_type("synthetic read failure")
                    return read()

                with patch.object(self.sched, "_read_schedule_file", side_effect=read_fails):
                    with self.assertRaises(error_type):
                        self._mutation(operation)
                self.assertEqual(calls, 1)
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(self._snapshot(), before)
                self.assertEqual(self._copies(), [])
                self.assertEqual(self.sched._read_schedule_file()["exports"],
                                 [{"path": "literal-existing-export"}])

    def test_direct_save_read_error_preserves_healthy_journal(self) -> None:
        self._healthy_fixture()
        before = self._snapshot()
        with patch.object(self.sched, "_read_schedule_file",
                          side_effect=PermissionError("synthetic read failure")):
            with self.assertRaises(PermissionError):
                self.sched._save_schedule({"enabled": False, "exports": []})
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._copies(), [])

    def test_repaired_second_read_preserves_existing_exports(self) -> None:
        for operation in ("configure", "cancel"):
            with self.subTest(operation=operation):
                self._healthy_fixture()
                raw = self.path.read_bytes()
                self.path.write_bytes(b"temporarily malformed")
                read = self.sched._read_schedule_file
                calls = 0

                def read_then_repair():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        try:
                            return read()
                        finally:
                            self.path.write_bytes(raw)
                    return read()

                with patch.object(self.sched, "_read_schedule_file", side_effect=read_then_repair):
                    status = self._mutation(operation)
                self.assertEqual(status["total_exports"], 1)
                self.assertEqual(status["last_export_ts"], "2026-01-01T12:00:00+00:00")
                self.assertEqual(json.loads(self.path.read_bytes())["exports"],
                                 [{"path": "literal-existing-export"}])
                self.assertEqual((self.dir / "literal-existing-export").read_bytes(), b"existing payload")
                self.assertEqual(self._copies(), [])

    def test_read_only_io_error_returns_defaults_without_writes(self) -> None:
        self._healthy_fixture()
        before = self._snapshot()
        with patch.object(self.sched, "_read_schedule_file",
                          side_effect=OSError("synthetic read failure")):
            status = self.sched.get_schedule_status()
            self.assertFalse(status["enabled"])
            self.assertEqual(status["total_exports"], 0)
            self.assertEqual(self.sched.list_exports(), [])
        self.assertEqual(self._snapshot(), before)

    def test_invalid_utf8_rotation_preserves_original(self) -> None:
        raw = b'{"exports": ["\xff"]}'
        self.path.write_bytes(raw)
        self.sched.cancel()
        self.assertEqual([copy.read_bytes() for copy in self._copies()], [raw])
        self.assertFalse(json.loads(self.path.read_bytes())["enabled"])

    def test_second_read_io_error_after_corruption_does_not_rotate(self) -> None:
        raw = b"malformed original"
        for operation in ("configure", "cancel"):
            with self.subTest(operation=operation):
                self.path.write_bytes(raw)
                before = self._snapshot()
                read = self.sched._read_schedule_file
                calls = 0

                def read_then_fail():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return read()
                    raise PermissionError("synthetic guard failure")

                with patch.object(self.sched, "_read_schedule_file", side_effect=read_then_fail):
                    with self.assertRaises(PermissionError):
                        self._mutation(operation)
                self.assertEqual(calls, 2)
                self.assertEqual(self._snapshot(), before)
                self.assertEqual(self._copies(), [])

    def test_unexpected_read_error_is_not_corruption(self) -> None:
        self._healthy_fixture()
        before = self._snapshot()
        with patch.object(self.sched, "_read_schedule_file",
                          side_effect=RuntimeError("synthetic unexpected failure")):
            for operation in ("configure", "cancel"):
                with self.subTest(operation=operation):
                    with self.assertRaises(RuntimeError):
                        self._mutation(operation)
            with self.assertRaises(RuntimeError):
                self.sched._save_schedule({"enabled": False})
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._copies(), [])

    def test_healthy_mutations_preserve_existing_exports(self) -> None:
        for operation in ("configure", "cancel"):
            with self.subTest(operation=operation):
                self._healthy_fixture()
                status = self._mutation(operation)
                self.assertEqual(status["total_exports"], 1)
                self.assertEqual(status["last_export_ts"], "2026-01-01T12:00:00+00:00")
                self.assertEqual(json.loads(self.path.read_bytes())["exports"],
                                 [{"path": "literal-existing-export"}])
                self.assertEqual((self.dir / "literal-existing-export").read_bytes(), b"existing payload")
                self.assertEqual(self._copies(), [])

    def test_missing_cancel_is_normal_fresh_start(self) -> None:
        status = self.sched.cancel()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["total_exports"], 0)
        self.assertEqual(self._copies(), [])
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_configure_keeps_corrupt_original(self) -> None:
        raw = b'{"enabled": true, "exports": [{"path": "/x"'
        self.path.write_bytes(raw)
        self.sched.configure(fmt="json", interval_hours=6, enabled=True)
        copies = self._copies()
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), raw)
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["enabled"])

    def test_cancel_keeps_corrupt_original(self) -> None:
        raw = b"not json at all"
        self.path.write_bytes(raw)
        self.sched.cancel()
        self.assertEqual([c.read_bytes() for c in self._copies()], [raw])

    def test_non_dict_json_is_treated_as_corrupt(self) -> None:
        self.path.write_bytes(b"[1, 2]")
        status = self.sched.get_schedule_status()
        self.assertFalse(status["enabled"])
        self.sched.cancel()
        self.assertEqual(len(self._copies()), 1)

    def test_status_read_does_not_create_copies(self) -> None:
        self.path.write_bytes(b"garbage")
        self.sched.get_schedule_status()
        self.sched.list_exports()
        self.assertEqual(self._copies(), [])

    def test_healthy_and_missing_files_make_no_copy(self) -> None:
        self.sched.configure(fmt="json", interval_hours=6, enabled=True)  # file missing before
        self.sched.configure(fmt="json", interval_hours=12, enabled=True)  # healthy before
        self.assertEqual(self._copies(), [])

    def test_retention_keeps_five_newest(self) -> None:
        for i in range(7):
            self.path.write_bytes(f"bad-{i}".encode())
            self.sched._save_schedule({"enabled": False})
        copies = self._copies()
        self.assertEqual(len(copies), 5)
        self.assertEqual(sorted(c.read_bytes() for c in copies), [f"bad-{i}".encode() for i in range(2, 7)])


if __name__ == "__main__":
    unittest.main()
