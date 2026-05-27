"""Tests for export path allowlist validation (W1176 / W1166-F1 HIGH).

Covers _resolve_export_dir, handle_export_obsidian and handle_batch_export
to ensure that arbitrary output_dir values outside the allowed roots are
rejected and that legitimate paths are accepted.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(data_dir: Path) -> HistoryService:
    store = StateStore(data_dir)
    return HistoryService(store=store)


def _add_item(svc: HistoryService, text: str = "test transcription") -> str:
    item = svc.store.add_history_item(text=text, paste_status="ok")
    return item.id


# ---------------------------------------------------------------------------
# _resolve_export_dir unit tests
# ---------------------------------------------------------------------------

class ResolveExportDirTestCase(unittest.TestCase):
    """Unit tests for the _resolve_export_dir path-allowlist helper."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.svc = _make_service(self.data_dir)

    def test_none_returns_none(self) -> None:
        """None output_dir → None (caller uses default)."""
        self.assertIsNone(self.svc._resolve_export_dir(None))

    def test_empty_string_returns_none(self) -> None:
        """Empty-string output_dir → None (caller uses default)."""
        self.assertIsNone(self.svc._resolve_export_dir(""))

    def test_data_dir_subpath_is_allowed(self) -> None:
        """Path inside data_dir is allowed."""
        target = self.data_dir / "exports" / "my_export"
        result = self.svc._resolve_export_dir(str(target))
        self.assertEqual(result, target.resolve())

    def test_data_dir_itself_is_allowed(self) -> None:
        """data_dir root itself is allowed."""
        result = self.svc._resolve_export_dir(str(self.data_dir))
        self.assertEqual(result, self.data_dir.resolve())

    def test_user_documents_is_allowed(self) -> None:
        """~/Documents is in the allowlist."""
        docs = Path.home() / "Documents"
        result = self.svc._resolve_export_dir(str(docs))
        self.assertEqual(result, docs.resolve())

    def test_user_documents_subpath_is_allowed(self) -> None:
        """~/Documents/KrabEar/exports is allowed."""
        subpath = Path.home() / "Documents" / "KrabEar" / "exports"
        result = self.svc._resolve_export_dir(str(subpath))
        self.assertEqual(result, subpath.resolve())

    def test_user_downloads_is_allowed(self) -> None:
        """~/Downloads is in the allowlist."""
        dl = Path.home() / "Downloads"
        result = self.svc._resolve_export_dir(str(dl))
        self.assertEqual(result, dl.resolve())

    def test_user_desktop_is_allowed(self) -> None:
        """~/Desktop is in the allowlist."""
        desktop = Path.home() / "Desktop"
        result = self.svc._resolve_export_dir(str(desktop))
        self.assertEqual(result, desktop.resolve())

    def test_tmp_is_allowed(self) -> None:
        """/tmp is allowed (for scripts/tests)."""
        result = self.svc._resolve_export_dir("/tmp/krab_test_export")
        self.assertIsNotNone(result)
        # Should resolve to something under /tmp or the system tempdir
        self.assertIn("tmp", str(result).lower())

    def test_rejects_traversal_to_etc_passwd(self) -> None:
        """Path to /etc is rejected with ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.svc._resolve_export_dir("/etc/passwd")
        self.assertIn("outside allowed directories", str(ctx.exception))

    def test_rejects_traversal_to_home_ssh(self) -> None:
        """~/.ssh is outside the allowlist and must be rejected."""
        ssh_dir = Path.home() / ".ssh"
        with self.assertRaises(ValueError) as ctx:
            self.svc._resolve_export_dir(str(ssh_dir))
        self.assertIn("outside allowed directories", str(ctx.exception))

    def test_rejects_traversal_to_library_keychains(self) -> None:
        """~/Library/Keychains is outside the allowlist and must be rejected."""
        keychains = Path.home() / "Library" / "Keychains"
        with self.assertRaises(ValueError) as ctx:
            self.svc._resolve_export_dir(str(keychains))
        self.assertIn("outside allowed directories", str(ctx.exception))

    def test_rejects_dotdot_traversal_through_data_dir(self) -> None:
        """A path crafted to look inside data_dir but resolving to /etc must be rejected."""
        # Use /etc/krab/../krab as the target — resolves to /etc/krab which is
        # outside all allowed roots regardless of data_dir location.
        evil = "/etc/krab_export_escape"
        with self.assertRaises(ValueError) as ctx:
            self.svc._resolve_export_dir(evil)
        self.assertIn("outside allowed directories", str(ctx.exception))

    def test_rejects_arbitrary_absolute_outside_allowlist(self) -> None:
        """/var/log is outside the allowlist."""
        with self.assertRaises(ValueError) as ctx:
            self.svc._resolve_export_dir("/var/log/krabear")
        self.assertIn("outside allowed directories", str(ctx.exception))

    def test_tilde_expands_correctly(self) -> None:
        """~ expansion works for ~/Downloads."""
        result = self.svc._resolve_export_dir("~/Downloads")
        self.assertEqual(result, (Path.home() / "Downloads").resolve())


# ---------------------------------------------------------------------------
# handle_export_obsidian integration tests
# ---------------------------------------------------------------------------

class ExportObsidianPathAllowlistTestCase(unittest.TestCase):
    """Integration tests for path allowlist in handle_export_obsidian."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.svc = _make_service(self.data_dir)
        self.item_id = _add_item(self.svc)

    def test_export_obsidian_rejects_traversal_to_etc_passwd(self) -> None:
        """handle_export_obsidian raises RuntimeError when output_dir=/etc."""
        with self.assertRaises((ValueError, RuntimeError)):
            self.svc.handle_export_obsidian(
                {"ids": [self.item_id], "output_dir": "/etc"}
            )

    def test_export_obsidian_rejects_traversal_to_home_ssh(self) -> None:
        """handle_export_obsidian raises RuntimeError when output_dir=~/.ssh."""
        ssh = str(Path.home() / ".ssh")
        with self.assertRaises((ValueError, RuntimeError)):
            self.svc.handle_export_obsidian(
                {"ids": [self.item_id], "output_dir": ssh}
            )

    def test_export_obsidian_allows_data_dir_default(self) -> None:
        """handle_export_obsidian with no output_dir writes to data_dir/transcripts."""
        result = self.svc.handle_export_obsidian({"ids": [self.item_id]})
        out_path = Path(result["file"])
        self.assertTrue(out_path.exists(), "Output file should be created")
        # Default dir is data_dir/transcripts
        self.assertTrue(
            str(out_path).startswith(str(self.data_dir)),
            f"Expected file inside data_dir={self.data_dir}, got {out_path}",
        )

    def test_export_obsidian_allows_explicit_data_dir_subpath(self) -> None:
        """Explicitly passing data_dir/custom_export as output_dir is allowed."""
        custom_dir = self.data_dir / "custom_export"
        result = self.svc.handle_export_obsidian(
            {"ids": [self.item_id], "output_dir": str(custom_dir)}
        )
        out_path = Path(result["file"])
        self.assertTrue(out_path.exists())
        self.assertTrue(str(out_path).startswith(str(custom_dir.resolve())))


# ---------------------------------------------------------------------------
# handle_batch_export integration tests
# ---------------------------------------------------------------------------

class ExportBundlePathAllowlistTestCase(unittest.TestCase):
    """Integration tests for path allowlist in handle_batch_export."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.svc = _make_service(self.data_dir)
        _add_item(self.svc, "hello world test")

    def test_export_bundle_rejects_absolute_outside_allowlist(self) -> None:
        """handle_batch_export raises when output_dir=/etc."""
        with self.assertRaises((ValueError, RuntimeError)):
            self.svc.handle_batch_export(
                {"formats": ["markdown"], "output_dir": "/etc"}
            )

    def test_export_bundle_rejects_ssh_dir(self) -> None:
        """handle_batch_export raises when output_dir=~/.ssh."""
        ssh = str(Path.home() / ".ssh")
        with self.assertRaises((ValueError, RuntimeError)):
            self.svc.handle_batch_export(
                {"formats": ["markdown"], "output_dir": ssh}
            )

    def test_export_bundle_allows_user_documents(self) -> None:
        """handle_batch_export writes to ~/Documents when given as output_dir."""
        docs = Path.home() / "Documents"
        if not docs.exists():
            self.skipTest("~/Documents does not exist on this machine")
        result = self.svc.handle_batch_export(
            {"formats": ["markdown"], "output_dir": str(docs)}
        )
        bundle_dir = Path(result["dir"])
        # Clean up the directory we just created
        try:
            import shutil
            shutil.rmtree(bundle_dir, ignore_errors=True)
        except Exception:
            pass
        self.assertTrue(
            str(bundle_dir).startswith(str(docs.resolve())),
            f"Expected bundle inside ~/Documents, got {bundle_dir}",
        )

    def test_export_bundle_allows_data_dir_default(self) -> None:
        """handle_batch_export with no output_dir writes to data_dir/exports."""
        result = self.svc.handle_batch_export({"formats": ["markdown"]})
        bundle_dir = Path(result["dir"]).resolve()
        data_dir_resolved = self.data_dir.resolve()
        try:
            bundle_dir.relative_to(data_dir_resolved)
        except ValueError:
            self.fail(
                f"Expected bundle inside data_dir={data_dir_resolved}, got {bundle_dir}"
            )


if __name__ == "__main__":
    unittest.main()
