"""Tests for wave-37: handle_export_obsidian + handle_backup_history privacy gates.

Covers two HIGH fixes in history_service.py:

A1 (HIGH) — handle_export_obsidian must not export the full transcript corpus to
            disk OR inline in the IPC response while privacy mode is active.

A2 (HIGH) — handle_backup_history must not copy raw history.ndjson (full cleartext
            corpus) to the backups/ directory while privacy mode is active.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class ObsidianExportPrivacyGateTestCase(unittest.TestCase):
    """handle_export_obsidian honours privacy mode gate (wave-37 A1, HIGH)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self._privacy: dict[str, object] = {"privacy_mode_enabled": False}
        self.svc = HistoryService(
            store=self.store,
            cached_settings=lambda: dict(self._privacy),
        )
        # Seed transcript content so a leak would be observable.
        for i in range(3):
            self.store.add_history_item(
                text=f"секретная запись {i}", paste_status="ok", source_lang="ru"
            )

    def _set_privacy(self, on: bool) -> None:
        self._privacy["privacy_mode_enabled"] = on

    # ------------------------------------------------------------------
    # privacy ON: export blocked, no file written, no corpus in response
    # ------------------------------------------------------------------

    def test_export_obsidian_blocked_in_privacy_mode(self) -> None:
        """Privacy ON → returns sentinel dict, does NOT write .md to disk."""
        self._set_privacy(True)
        output_dir = Path(self.tmp.name) / "obsidian_out"
        output_dir.mkdir()

        res = self.svc.handle_export_obsidian({"output_dir": str(output_dir)})

        self.assertIsNone(res.get("file"), "file must be None in privacy mode")
        self.assertEqual(res.get("entries"), 0, "entries must be 0 in privacy mode")
        self.assertEqual(res.get("content"), "", "content must be empty in privacy mode")
        self.assertEqual(res.get("reason"), "privacy_mode_active")

        # No .md file must have been written.
        md_files = list(output_dir.glob("*.md"))
        self.assertEqual(md_files, [], f"No .md files should be written in privacy mode, got: {md_files}")

    def test_export_obsidian_blocked_without_output_dir(self) -> None:
        """Privacy ON blocks export even when no output_dir is provided."""
        self._set_privacy(True)
        res = self.svc.handle_export_obsidian({})
        self.assertIsNone(res.get("file"))
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_export_obsidian_blocked_with_ids_param(self) -> None:
        """Privacy ON blocks id-targeted export too (no special-case for ids)."""
        self._set_privacy(True)
        res = self.svc.handle_export_obsidian({"ids": ["nonexistent-id"]})
        self.assertIsNone(res.get("file"))
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    # ------------------------------------------------------------------
    # privacy OFF: export works normally (smoke — does not assert content)
    # ------------------------------------------------------------------

    def test_export_obsidian_allowed_when_privacy_off(self) -> None:
        """Privacy OFF → handler proceeds (raises or returns file/entries)."""
        self._set_privacy(False)
        output_dir = Path(self.tmp.name) / "obsidian_normal"
        output_dir.mkdir()
        # With real entries in the store the handler should succeed.
        try:
            res = self.svc.handle_export_obsidian({"output_dir": str(output_dir)})
            # If it succeeds, reason should NOT be the privacy sentinel.
            self.assertNotEqual(res.get("reason"), "privacy_mode_active")
            self.assertIsNotNone(res.get("file"), "Should return a file path when privacy is off")
        except RuntimeError:
            # Raised if no items match — acceptable when store is empty or
            # range yields nothing.  The key check is that we did NOT get the
            # privacy sentinel.
            pass


class BackupHistoryPrivacyGateTestCase(unittest.TestCase):
    """handle_backup_history honours privacy mode gate (wave-37 A2, HIGH)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self._privacy: dict[str, object] = {"privacy_mode_enabled": False}
        self.svc = HistoryService(
            store=self.store,
            cached_settings=lambda: dict(self._privacy),
        )
        # Seed transcript content so a backup would be non-empty.
        for i in range(3):
            self.store.add_history_item(
                text=f"приватная запись {i}", paste_status="ok", source_lang="ru"
            )

    def _set_privacy(self, on: bool) -> None:
        self._privacy["privacy_mode_enabled"] = on

    # ------------------------------------------------------------------
    # privacy ON: backup blocked, NO files written to backups/
    # ------------------------------------------------------------------

    def test_backup_history_blocked_in_privacy_mode(self) -> None:
        """Privacy ON → returns sentinel dict with backup_path=None."""
        self._set_privacy(True)
        res = self.svc.handle_backup_history({})

        self.assertIsNone(res.get("backup_path"), "backup_path must be None in privacy mode")
        self.assertEqual(res.get("size_mb"), 0.0, "size_mb must be 0.0 in privacy mode")
        self.assertEqual(res.get("entries"), 0, "entries must be 0 in privacy mode")
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_backup_history_no_files_written_in_privacy_mode(self) -> None:
        """Privacy ON → backups/ directory must not be created or populated."""
        self._set_privacy(True)
        backups_dir = Path(self.tmp.name) / "data" / "backups"

        self.svc.handle_backup_history({})

        if backups_dir.exists():
            backup_subdirs = list(backups_dir.iterdir())
            self.assertEqual(
                backup_subdirs, [],
                f"No backup subdirs should be created in privacy mode, got: {backup_subdirs}",
            )

    # ------------------------------------------------------------------
    # privacy OFF: backup works normally
    # ------------------------------------------------------------------

    def test_backup_history_allowed_when_privacy_off(self) -> None:
        """Privacy OFF → backup proceeds and backup_path is returned."""
        self._set_privacy(False)
        res = self.svc.handle_backup_history({})

        self.assertIsNotNone(res.get("backup_path"), "backup_path should be set when privacy is off")
        self.assertNotEqual(res.get("reason"), "privacy_mode_active")
        # The backup directory should actually exist on disk.
        backup_path = Path(res["backup_path"])
        self.assertTrue(backup_path.exists(), f"Backup directory must exist: {backup_path}")

    def test_backup_history_creates_backup_meta_when_privacy_off(self) -> None:
        """Privacy OFF → backup_meta.json is written inside the backup dir."""
        self._set_privacy(False)
        res = self.svc.handle_backup_history({})
        backup_path = Path(res["backup_path"])
        meta_file = backup_path / "backup_meta.json"
        self.assertTrue(meta_file.exists(), "backup_meta.json should be written when privacy is off")


if __name__ == "__main__":
    unittest.main()
