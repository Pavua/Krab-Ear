"""Tests for W1776 security fixes in ExportScheduler.

Covers:
  (a) Path-traversal guard in _prune_old_exports — entries with paths outside
      data_dir are silently skipped and NOT unlinked.
  (b) Path-traversal guard in list_exports — entries with paths outside
      data_dir are omitted from the result.
  (c) Privacy-mode fail-closed path — if settings_provider raises during the
      privacy-mode check, check_and_export() returns
      {"exported": False, "reason": "privacy_mode_error"}.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.export_scheduler import ExportScheduler


def _make_scheduler(data_dir: Path, settings_provider=None) -> ExportScheduler:
    """Return an ExportScheduler wired to *data_dir*."""
    return ExportScheduler(data_dir=data_dir, settings_provider=settings_provider)


def _write_schedule(data_dir: Path, entries: list[dict], enabled: bool = True) -> None:
    schedule_path = data_dir / "export_schedule.json"
    schedule_path.write_text(
        json.dumps({"exports": entries, "interval_hours": 24, "enabled": enabled}),
        encoding="utf-8",
    )


class TestPrunePathTraversalGuard(unittest.TestCase):
    """_prune_old_exports must skip (not unlink) paths outside data_dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.scheduler = _make_scheduler(self.data_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_prune(self, entries: list[dict]) -> dict:
        schedule = {"exports": entries}
        return self.scheduler._prune_old_exports(schedule)

    def test_absolute_evil_path_is_skipped(self) -> None:
        """/etc/passwd must be dropped silently, not unlinked."""
        evil_path = "/etc/passwd"
        with patch.object(Path, "unlink") as mock_unlink:
            result = self._run_prune([{"path": evil_path, "ts": "2000-01-01T00:00:00"}])

        # unlink must never have been called on the evil path
        for call in mock_unlink.call_args_list:
            args = call[0]
            if args:
                self.assertNotEqual(str(args[0]), evil_path)

        # The evil path must not appear in the pruned export list either.
        remaining = [e["path"] for e in result.get("exports", [])]
        self.assertNotIn(evil_path, remaining)

    def test_dotdot_traversal_path_is_skipped(self) -> None:
        """A ../../evil path is resolved and blocked."""
        evil_relative = str(self.data_dir / ".." / ".." / "evil_file")
        with patch.object(Path, "unlink") as mock_unlink:
            result = self._run_prune([{"path": evil_relative, "ts": "2000-01-01T00:00:00"}])

        for call in mock_unlink.call_args_list:
            args = call[0]
            if args:
                self.assertNotIn("evil_file", str(args[0]))

        remaining = [e["path"] for e in result.get("exports", [])]
        self.assertNotIn(evil_relative, remaining)

    def test_existing_valid_path_is_kept(self) -> None:
        """An export file inside data_dir that exists is retained in the schedule."""
        exports_dir = self.data_dir / "auto_exports"
        exports_dir.mkdir()
        valid_file = exports_dir / "export_2025.json"
        valid_file.write_text("{}", encoding="utf-8")

        result = self._run_prune([{"path": str(valid_file), "ts": "2025-06-01T10:00:00"}])

        remaining = [e["path"] for e in result.get("exports", [])]
        self.assertIn(str(valid_file), remaining)

    def test_missing_valid_path_is_pruned_from_schedule(self) -> None:
        """A legitimate but missing file is removed from the schedule."""
        missing_path = str(self.data_dir / "auto_exports" / "gone.json")
        result = self._run_prune([{"path": missing_path, "ts": "2025-01-01T00:00:00"}])

        remaining = [e["path"] for e in result.get("exports", [])]
        self.assertNotIn(missing_path, remaining)


class TestListExportsPathTraversalGuard(unittest.TestCase):
    """list_exports must omit entries whose path is outside data_dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.scheduler = _make_scheduler(self.data_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_absolute_evil_path_not_returned(self) -> None:
        """/etc/passwd must not appear in list_exports output."""
        evil_path = "/etc/passwd"
        _write_schedule(self.data_dir, [{"path": evil_path, "ts": "2025-01-01T00:00:00"}])
        # Patch exists so the traversal guard — not the existence check — is the filter.
        with patch.object(Path, "exists", return_value=True):
            result = self.scheduler.list_exports()
        returned_paths = [e["path"] for e in result]
        self.assertNotIn(evil_path, returned_paths)

    def test_dotdot_path_not_returned(self) -> None:
        """A ../../evil path must be resolved and blocked."""
        evil_relative = str(self.data_dir / ".." / ".." / "evil")
        _write_schedule(self.data_dir, [{"path": evil_relative, "ts": "2025-01-01T00:00:00"}])
        with patch.object(Path, "exists", return_value=True):
            result = self.scheduler.list_exports()
        returned_paths = [e["path"] for e in result]
        self.assertNotIn(evil_relative, returned_paths)

    def test_valid_path_inside_data_dir_is_returned(self) -> None:
        """An existing file inside data_dir is returned by list_exports."""
        exports_dir = self.data_dir / "auto_exports"
        exports_dir.mkdir()
        valid_path = str(exports_dir / "export.json")
        Path(valid_path).write_text("{}", encoding="utf-8")
        _write_schedule(self.data_dir, [{"path": valid_path, "ts": "2025-06-01T10:00:00"}])

        result = self.scheduler.list_exports()

        returned_paths = [e["path"] for e in result]
        self.assertIn(valid_path, returned_paths)


class TestPrivacyModeFailClosed(unittest.TestCase):
    """If settings_provider raises, check_and_export must return privacy_mode_error."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_settings_provider_raises_returns_privacy_mode_error(self) -> None:
        """RuntimeError from settings_provider → privacy_mode_error (fail-closed)."""
        def _raise():
            raise RuntimeError("settings unavailable")

        scheduler = _make_scheduler(self.data_dir, settings_provider=_raise)
        # Write a minimal schedule with enabled=True so we reach the provider call.
        _write_schedule(self.data_dir, [], enabled=True)

        store = MagicMock()
        result = scheduler.check_and_export(store)

        self.assertIsNotNone(result)
        self.assertFalse(result.get("exported", True))
        self.assertEqual(result.get("reason"), "privacy_mode_error")

    def test_settings_provider_exception_subclass_returns_privacy_mode_error(self) -> None:
        """Any Exception subclass from settings_provider is caught → privacy_mode_error."""
        def _raise():
            raise ValueError("bad settings")

        scheduler = _make_scheduler(self.data_dir, settings_provider=_raise)
        _write_schedule(self.data_dir, [], enabled=True)

        store = MagicMock()
        result = scheduler.check_and_export(store)

        self.assertFalse(result.get("exported", True))
        self.assertEqual(result.get("reason"), "privacy_mode_error")

    def test_privacy_mode_active_is_not_confused_with_error(self) -> None:
        """When privacy_mode_enabled=True, reason is privacy_mode_active (not error)."""
        scheduler = _make_scheduler(
            self.data_dir,
            settings_provider=lambda: {"privacy_mode_enabled": True},
        )
        _write_schedule(self.data_dir, [], enabled=True)

        store = MagicMock()
        result = scheduler.check_and_export(store)

        self.assertFalse(result.get("exported", True))
        self.assertEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main()
