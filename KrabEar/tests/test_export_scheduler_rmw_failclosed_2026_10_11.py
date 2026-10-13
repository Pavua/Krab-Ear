"""RMW сохраняет exports при I/O-сбое и повторном здоровом чтении.

Доказанная corruption сохраняется move-aside с прежним retention=5;
read-only сбои не меняют исходный журнал или payload-файлы.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
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

    def test_auto_checkpoint_transient_reread_keeps_payload_tracked(self) -> None:
        for error_type in (PermissionError, OSError):
            with self.subTest(error_type=error_type):
                root = self.dir / error_type.__name__
                root.mkdir()
                sched = ExportScheduler(root, max_exports=2,
                                        plaintext_export_authorizer=off_authorizer())
                old = root / "literal-old-export.json"
                old.write_bytes(b"literal old payload")
                old_entry = {"path": str(old), "ts": "20260101_000000"}
                raw = json.dumps({"enabled": True, "format": "json", "interval_hours": 1,
                                  "last_export_ts": "2026-01-01T00:00:00+00:00",
                                  "exports": [old_entry]}).encode()
                sched._schedule_path.write_bytes(raw)
                read = sched._read_schedule_file
                calls = 0
                tick = datetime(2026, 10, 11, tzinfo=timezone.utc)

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return tick

                def guard_read_fails_once():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        self.assertEqual(sched._schedule_path.read_bytes(), raw)
                        payloads = list(sched.exports_dir.glob("*.json"))
                        self.assertEqual(len(payloads), 1)
                        self.assertEqual(payloads[0].read_bytes(), b"literal authorized export payload")
                        raise error_type("synthetic checkpoint read failure")
                    return read()

                with patch("backend.export_scheduler.datetime", Clock), \
                        patch.object(sched, "_generate_content", return_value="literal authorized export payload"), \
                        patch.object(sched, "_read_schedule_file", side_effect=guard_read_fails_once):
                    first = sched.check_and_export(object())
                    first_path = Path(first["path"])
                    saved = json.loads(sched._schedule_path.read_bytes())
                    self.assertEqual(saved["exports"], [old_entry, first])
                    self.assertEqual(saved["last_export_ts"], tick.isoformat())
                    self.assertEqual({Path(e["path"]) for e in sched.list_exports()}, {old, first_path})
                    self.assertEqual(list(root.glob("*.corrupt-*")), [])
                    self.assertEqual(list(root.rglob("*.tmp")), [])
                    tick += timedelta(hours=2)
                    second = sched.check_and_export(object())
                    second_path = Path(second["path"])
                    self.assertEqual(json.loads(sched._schedule_path.read_bytes())["exports"], [first, second])
                    self.assertTrue(first_path.exists())
                    self.assertFalse(old.exists())
                    tick += timedelta(hours=2)
                    third = sched.check_and_export(object())
                    self.assertFalse(first_path.exists())
                    self.assertEqual(json.loads(sched._schedule_path.read_bytes())["exports"], [second, third])
                    self.assertEqual({Path(e["path"]) for e in sched.list_exports()},
                                     {second_path, Path(third["path"])})

    def test_auto_initial_unreadable_schedule_never_writes_payload(self) -> None:
        self._healthy_fixture()
        before = self._snapshot()
        for error_type in (PermissionError, OSError):
            with self.subTest(error_type=error_type), \
                    patch.object(self.sched, "_read_schedule_file", side_effect=error_type("initial read failure")):
                self.assertIsNone(self.sched.check_and_export(object()))
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self.sched.exports_dir.exists())

    def _auto_fixture(self, root: Path) -> ExportScheduler:
        root.mkdir(exist_ok=True)
        sched = ExportScheduler(root, plaintext_export_authorizer=self.sched._plaintext_export_authorizer)
        sched._schedule_path.write_bytes(b'{"enabled": true, "exports": []}')
        return sched

    def test_auto_checkpoint_genuine_corruption_still_rotates(self) -> None:
        for idx, raw in enumerate((b"malformed", b"[1, 2]", b"\xff")):
            with self.subTest(raw=raw):
                root = self.dir / str(idx)
                sched = self._auto_fixture(root)
                export = sched._do_export

                def export_then_corrupt(*args):
                    entry = export(*args)
                    sched._schedule_path.write_bytes(raw)
                    return entry

                with patch.object(sched, "_generate_content", return_value="literal payload"), \
                        patch.object(sched, "_do_export", side_effect=export_then_corrupt):
                    entry = sched.check_and_export(object())
                self.assertEqual([p.read_bytes() for p in root.glob("*.corrupt-*")], [raw])
                self.assertEqual(json.loads(sched._schedule_path.read_bytes())["exports"], [entry])
                self.assertEqual(Path(entry["path"]).read_bytes(), b"literal payload")

    def test_auto_checkpoint_rotation_failure_still_raises(self) -> None:
        sched = self._auto_fixture(self.dir)
        raw = b"corrupt after payload"
        export = sched._do_export
        replace = os.replace

        def export_then_corrupt(*args):
            entry = export(*args)
            sched._schedule_path.write_bytes(raw)
            return entry

        def fail_rotation(src, dst):
            if Path(src) == sched._schedule_path:
                raise OSError("synthetic rotation failure")
            return replace(src, dst)

        with patch.object(sched, "_generate_content", return_value="literal payload"), \
                patch.object(sched, "_do_export", side_effect=export_then_corrupt), \
                patch("backend.export_scheduler.os.replace", side_effect=fail_rotation):
            with self.assertRaisesRegex(OSError, "synthetic rotation failure"):
                sched.check_and_export(object())
        self.assertEqual(sched._schedule_path.read_bytes(), raw)
        self.assertEqual(self._copies(), [])
        self.assertEqual([p.read_bytes() for p in sched.exports_dir.glob("*.json")], [b"literal payload"])
        self.assertEqual(list(self.dir.rglob("*.tmp")), [])

    def test_auto_checkpoint_retention_failure_still_raises(self) -> None:
        sched = self._auto_fixture(self.dir)
        raw = b"corrupt after payload"
        export = sched._do_export
        unlink = Path.unlink
        oldest = self.dir / "export_schedule.json.corrupt-000"
        for idx in range(5):
            (self.dir / f"export_schedule.json.corrupt-{idx:03}").write_bytes(b"old corruption")

        def export_then_corrupt(*args):
            entry = export(*args)
            sched._schedule_path.write_bytes(raw)
            return entry

        def fail_retention(path, *args, **kwargs):
            if path == oldest:
                raise OSError("synthetic retention failure")
            return unlink(path, *args, **kwargs)

        with patch.object(sched, "_generate_content", return_value="literal payload"), \
                patch.object(sched, "_do_export", side_effect=export_then_corrupt), \
                patch.object(Path, "unlink", new=fail_retention):
            with self.assertRaisesRegex(OSError, "synthetic retention failure"):
                sched.check_and_export(object())
        self.assertFalse(sched._schedule_path.exists())
        self.assertIn(raw, [p.read_bytes() for p in self._copies()])
        self.assertEqual([p.read_bytes() for p in sched.exports_dir.glob("*.json")], [b"literal payload"])
        self.assertEqual(list(self.dir.rglob("*.tmp")), [])

    def test_auto_checkpoint_write_failure_still_raises(self) -> None:
        sched = self._auto_fixture(self.dir)
        raw = sched._schedule_path.read_bytes()
        write_text = Path.write_text

        def fail_checkpoint_write(path, *args, **kwargs):
            if path == sched._schedule_path.with_suffix(".json.tmp"):
                raise OSError("synthetic checkpoint write failure")
            return write_text(path, *args, **kwargs)

        with patch.object(sched, "_generate_content", return_value="literal payload"), \
                patch.object(Path, "write_text", new=fail_checkpoint_write):
            with self.assertRaisesRegex(OSError, "synthetic checkpoint write failure"):
                sched.check_and_export(object())
        self.assertEqual(sched._schedule_path.read_bytes(), raw)
        self.assertEqual(self._copies(), [])
        self.assertEqual([p.read_bytes() for p in sched.exports_dir.glob("*.json")], [b"literal payload"])

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
