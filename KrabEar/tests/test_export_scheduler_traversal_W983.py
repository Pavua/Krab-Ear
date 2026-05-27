"""W978 F2 HIGH — ExportScheduler output_dir path-traversal guard tests.

Tests that arbitrary paths outside data_dir are rejected at:
  1. configure() / configure_auto_export — before persisting the schedule.
  2. _effective_output_dir() — when resolving a stored schedule at export time.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.export_scheduler import ExportScheduler  # noqa: E402


class TestOutputDirTraversalGuard(unittest.TestCase):
    """Path-traversal guard on output_dir — W978 F2 HIGH."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._td.name)
        self.scheduler = ExportScheduler(self.data_dir)

    def tearDown(self) -> None:
        self._td.cleanup()

    # ------------------------------------------------------------------
    # configure() — validation BEFORE persisting
    # ------------------------------------------------------------------

    def test_output_dir_outside_data_dir_rejected_at_configure(self) -> None:
        """configure() must raise ValueError when output_dir is outside data_dir."""
        with self.assertRaises(ValueError) as ctx:
            self.scheduler.configure("json", output_dir="/tmp/evil_export")
        self.assertIn("data_dir", str(ctx.exception))

    def test_output_dir_etc_rejected_at_configure(self) -> None:
        """/etc must be rejected."""
        with self.assertRaises(ValueError):
            self.scheduler.configure("json", output_dir="/etc")

    def test_output_dir_dot_dot_escape_rejected_at_configure(self) -> None:
        """Relative traversal that resolves outside data_dir must be rejected."""
        # Construct a path that starts inside data_dir but uses .. to escape
        evil = str(self.data_dir / ".." / "outside")
        with self.assertRaises(ValueError):
            self.scheduler.configure("json", output_dir=evil)

    def test_output_dir_inside_data_dir_accepted_at_configure(self) -> None:
        """A subdirectory inside data_dir must be accepted."""
        valid_dir = str(self.data_dir / "exports" / "sub")
        result = self.scheduler.configure("json", output_dir=valid_dir)
        self.assertTrue(result["enabled"])
        # Stored path should be the resolved form inside data_dir
        stored = Path(result["output_dir"])
        self.assertTrue(stored.is_relative_to(self.data_dir.resolve()))

    def test_output_dir_none_accepted_at_configure(self) -> None:
        """output_dir=None (use default exports_dir) must always be accepted."""
        result = self.scheduler.configure("json", output_dir=None)
        self.assertIsNone(result["output_dir"])

    def test_configure_does_not_persist_invalid_path(self) -> None:
        """Schedule file must not be updated when output_dir is invalid."""
        import json as _json
        schedule_path = self.data_dir / ExportScheduler.SCHEDULE_FILENAME
        # Write an initial schedule
        self.scheduler.configure("json")
        mtime_before = schedule_path.stat().st_mtime

        with self.assertRaises(ValueError):
            self.scheduler.configure("json", output_dir="/etc/passwd")

        # Schedule file must not have been touched
        mtime_after = schedule_path.stat().st_mtime
        self.assertEqual(mtime_before, mtime_after)

    # ------------------------------------------------------------------
    # _effective_output_dir() — validation at export time
    # ------------------------------------------------------------------

    def test_effective_output_dir_raises_on_escape(self) -> None:
        """_effective_output_dir must raise ValueError for persisted escaping paths."""
        # Directly inject a bad path into the schedule (simulate tampered file)
        import json as _json
        schedule = {
            "enabled": True,
            "format": "json",
            "interval_hours": 24,
            "output_dir": "/etc/krab_evil",
            "last_export_ts": None,
            "exports": [],
        }
        schedule_path = self.data_dir / ExportScheduler.SCHEDULE_FILENAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        schedule_path.write_text(_json.dumps(schedule), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            self.scheduler._effective_output_dir(schedule)
        self.assertIn("data_dir", str(ctx.exception))

    def test_effective_output_dir_safe_path_ok(self) -> None:
        """_effective_output_dir returns resolved Path for a valid sub-dir."""
        sub = str(self.data_dir / "safe_exports")
        schedule = {"output_dir": sub}
        result = self.scheduler._effective_output_dir(schedule)
        # result is an absolute resolved path inside data_dir
        self.assertTrue(result.is_absolute())
        self.assertTrue(result.is_relative_to(self.data_dir.resolve()))

    def test_effective_output_dir_none_returns_exports_dir(self) -> None:
        """_effective_output_dir returns default exports_dir when output_dir is None."""
        schedule = {"output_dir": None}
        result = self.scheduler._effective_output_dir(schedule)
        self.assertEqual(result, self.scheduler.exports_dir)

    # ------------------------------------------------------------------
    # _validate_output_dir — unit tests of the guard method directly
    # ------------------------------------------------------------------

    def test_validate_output_dir_rejects_root(self) -> None:
        with self.assertRaises(ValueError):
            self.scheduler._validate_output_dir("/")

    def test_validate_output_dir_rejects_home_escape(self) -> None:
        with self.assertRaises(ValueError):
            self.scheduler._validate_output_dir(str(Path.home()))

    def test_validate_output_dir_accepts_subdir(self) -> None:
        p = self.scheduler._validate_output_dir(str(self.data_dir / "a" / "b"))
        self.assertTrue(p.is_absolute())
        self.assertTrue(p.is_relative_to(self.data_dir.resolve()))

    def test_validate_output_dir_expands_tilde_inside_data_dir(self) -> None:
        """~ that resolves outside data_dir is still rejected."""
        with self.assertRaises(ValueError):
            self.scheduler._validate_output_dir("~/Documents")


if __name__ == "__main__":
    unittest.main()
