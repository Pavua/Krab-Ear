"""Tests for export path allowlist restored in W1532 (SECURITY HIGH regression fix).

W1432 introduced _EXPORT_ALLOWED_ROOTS + _is_safe_export_dir() to prevent
write-anywhere path traversal.  W1497 cherry-pick train inadvertently reverted
this guard; W1532 restores it.

Covers:
- test_export_inside_allowed_root_succeeds
- test_export_outside_allowed_root_raises
- test_export_parent_traversal_rejected
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import _is_safe_export_dir, _EXPORT_ALLOWED_ROOTS


class TestIsSafeExportDir(unittest.TestCase):
    """Unit tests for the module-level _is_safe_export_dir() helper."""

    def test_export_inside_allowed_root_succeeds(self) -> None:
        """A sub-directory of ~/Documents must be accepted."""
        docs_root = Path("~/Documents").expanduser().resolve()
        target = str(docs_root / "KrabEar" / "exports")
        self.assertTrue(_is_safe_export_dir(target))

    def test_export_inside_krab_ear_data_succeeds(self) -> None:
        """The default ~/.krab_ear_data subtree must be accepted."""
        target = str(Path("~/.krab_ear_data").expanduser().resolve() / "transcripts")
        self.assertTrue(_is_safe_export_dir(target))

    def test_export_inside_app_support_succeeds(self) -> None:
        """~/Library/Application Support/KrabEar subtree must be accepted."""
        root = Path("~/Library/Application Support/KrabEar").expanduser().resolve()
        target = str(root / "exports" / "2026")
        self.assertTrue(_is_safe_export_dir(target))

    def test_export_outside_allowed_root_raises(self) -> None:
        """An absolute path outside every allowed root must be rejected."""
        # /etc is clearly outside the allowlist (W1707: /tmp IS allowed per W1432 spec)
        self.assertFalse(_is_safe_export_dir("/etc/evil_export"))

    def test_export_etc_rejected(self) -> None:
        """/etc must be rejected."""
        self.assertFalse(_is_safe_export_dir("/etc"))

    def test_export_root_rejected(self) -> None:
        """Filesystem root / must be rejected."""
        self.assertFalse(_is_safe_export_dir("/"))

    def test_export_parent_traversal_rejected(self) -> None:
        """A path that traverses out of an allowed root via .. must be rejected.

        e.g. ~/Documents/../../etc must resolve outside every allowed root.
        """
        # Construct a path that starts inside Documents but climbs out
        traversal = str(Path("~/Documents").expanduser() / ".." / ".." / "etc")
        result = _is_safe_export_dir(traversal)
        # After resolution ~/Documents/../../etc == /Users/../etc = /etc (outside)
        self.assertFalse(result)

    def test_export_home_dir_itself_rejected(self) -> None:
        """~/ (home directory itself) is NOT in the allowlist."""
        home = str(Path("~").expanduser().resolve())
        self.assertFalse(_is_safe_export_dir(home))

    def test_export_invalid_path_string_rejected(self) -> None:
        """An empty string path must be rejected without raising."""
        self.assertFalse(_is_safe_export_dir(""))


class TestHandleExportObsidianAllowlist(unittest.TestCase):
    """Integration-style tests: handle_export_obsidian raises on bad output_dir."""

    def _make_svc(self, tmp_dir: Path):
        from backend.history_service import HistoryService
        from backend.state_store import StateStore

        store = StateStore(tmp_dir)
        # Seed one history item so the handler doesn't error on empty store
        store.add_history_item(text="Test transcript", paste_status="ok")
        return HistoryService(store=store)

    def test_export_obsidian_outside_allowed_root_raises(self) -> None:
        """handle_export_obsidian must raise ValueError for paths outside allowed roots.

        W1707: /tmp IS allowed (per W1432); use /var/evil which is clearly outside.
        """
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._make_svc(Path(tmp) / "data")
            with self.assertRaises(ValueError) as ctx:
                svc.handle_export_obsidian({"output_dir": "/var/evil"})
            self.assertIn("outside allowed", str(ctx.exception))

    def test_export_obsidian_inside_allowed_root_succeeds(self) -> None:
        """handle_export_obsidian must succeed when output_dir is inside ~/Documents."""
        docs = Path("~/Documents").expanduser().resolve()
        if not docs.exists():
            self.skipTest("~/Documents does not exist on this machine")

        with tempfile.TemporaryDirectory(dir=docs) as tmp_subdir:
            with tempfile.TemporaryDirectory() as data_tmp:
                svc = self._make_svc(Path(data_tmp) / "data")
                result = svc.handle_export_obsidian({"output_dir": tmp_subdir})
                self.assertIn("file", result)
                self.assertIn("entries", result)
                self.assertGreater(result["entries"], 0)
                # Verify file was created inside the allowed dir
                out_file = Path(result["file"])
                self.assertTrue(out_file.exists())
                # Cleanup
                out_file.unlink(missing_ok=True)


class TestHandleBatchExportAllowlist(unittest.TestCase):
    """Integration-style tests: handle_batch_export raises on bad output_dir."""

    def _make_svc(self, tmp_dir: Path):
        from backend.history_service import HistoryService
        from backend.state_store import StateStore

        store = StateStore(tmp_dir)
        store.add_history_item(text="Batch export test", paste_status="ok")
        return HistoryService(store=store)

    def test_batch_export_outside_allowed_root_raises(self) -> None:
        """handle_batch_export must raise ValueError for paths outside allowed roots.

        W1707: /tmp IS allowed (per W1432); use /var/evil which is clearly outside.
        """
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._make_svc(Path(tmp) / "data")
            with self.assertRaises(ValueError) as ctx:
                svc.handle_batch_export({"output_dir": "/var/evil_bundle"})
            self.assertIn("outside allowed", str(ctx.exception))

    def test_batch_export_parent_traversal_rejected(self) -> None:
        """handle_batch_export must reject parent traversal in output_dir."""
        # ~/Documents/../../etc resolves to /etc (outside allowed roots)
        traversal = str(Path("~/Documents").expanduser() / ".." / ".." / "etc")
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._make_svc(Path(tmp) / "data")
            with self.assertRaises(ValueError):
                svc.handle_batch_export({"output_dir": traversal})


if __name__ == "__main__":
    unittest.main()
