"""Corrupt export_schedule.json must be moved aside before it is rewritten.

``_load_schedule`` swallowed a parse failure and returned defaults; ``configure`` /
``cancel`` then overwrote the file, dropping ``exports`` — the list that drives pruning —
so previously written plaintext export files were never tracked or pruned again.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

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
