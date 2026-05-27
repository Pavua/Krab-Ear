"""Tests for module-level export path allowlist (W1432 / W1426-F3 HIGH).

Covers _is_safe_export_dir and _EXPORT_ALLOWED_ROOTS (module-level helpers
added in W1432) as well as the path-traversal guards in handle_export_obsidian
and handle_batch_export.

W1176 already added _resolve_export_dir (instance method) with broader tests.
This file specifically validates the module-level boolean predicate and four
key scenarios from the W1432 spec.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import (
    HistoryService,
    _EXPORT_ALLOWED_ROOTS,
    _is_safe_export_dir,
)
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
# _is_safe_export_dir unit tests
# ---------------------------------------------------------------------------

class IsSafeExportDirTestCase(unittest.TestCase):
    """Unit tests for the module-level _is_safe_export_dir boolean predicate."""

    # W1432 spec test 1
    def test_export_obsidian_rejects_ssh_path(self) -> None:
        """~/.ssh is outside the allowlist and must not be considered safe."""
        ssh_dir = Path.home() / ".ssh"
        self.assertFalse(
            _is_safe_export_dir(ssh_dir),
            f"~/.ssh must be rejected but _is_safe_export_dir returned True for {ssh_dir}",
        )

    # W1432 spec test 2
    def test_export_obsidian_accepts_documents_path(self) -> None:
        """~/Documents is in the allowlist and must be considered safe."""
        docs_dir = Path.home() / "Documents" / "KrabEar"
        self.assertTrue(
            _is_safe_export_dir(docs_dir),
            f"~/Documents/KrabEar must be accepted but _is_safe_export_dir returned False for {docs_dir}",
        )

    # W1432 spec test 3
    def test_batch_export_rejects_traversal_path(self) -> None:
        """Path traversal like ../../../../etc must be rejected."""
        # Construct a traversal that resolves outside the home dir
        traversal = Path.home() / "Documents" / "../../../../etc"
        self.assertFalse(
            _is_safe_export_dir(traversal),
            f"Traversal path must be rejected but _is_safe_export_dir returned True for {traversal} "
            f"(resolves to {traversal.expanduser().resolve()})",
        )

    # W1432 spec test 4
    def test_export_tmp_allowed(self) -> None:
        """/tmp is in the allowlist and must be considered safe."""
        tmp_subdir = Path("/tmp") / "krab_export_test"
        self.assertTrue(
            _is_safe_export_dir(tmp_subdir),
            f"/tmp/krab_export_test must be accepted but _is_safe_export_dir returned False",
        )

    # Additional coverage: ensure the constant itself is importable and non-empty
    def test_export_allowed_roots_non_empty(self) -> None:
        """_EXPORT_ALLOWED_ROOTS must define at least 3 roots."""
        self.assertGreaterEqual(
            len(_EXPORT_ALLOWED_ROOTS), 3,
            "_EXPORT_ALLOWED_ROOTS should have at least Documents, Desktop, Downloads",
        )

    def test_private_tmp_allowed(self) -> None:
        """/private/tmp (macOS symlink target for /tmp) is allowed."""
        self.assertTrue(
            _is_safe_export_dir(Path("/private/tmp/krab_test")),
            "/private/tmp/krab_test must be accepted",
        )

    def test_desktop_allowed(self) -> None:
        """~/Desktop is in the allowlist and must be considered safe."""
        self.assertTrue(
            _is_safe_export_dir(Path.home() / "Desktop"),
            "~/Desktop must be accepted",
        )

    def test_library_keychains_rejected(self) -> None:
        """~/Library/Keychains must not be considered safe."""
        keychains = Path.home() / "Library" / "Keychains"
        self.assertFalse(
            _is_safe_export_dir(keychains),
            "~/Library/Keychains must be rejected",
        )

    def test_etc_rejected(self) -> None:
        """/etc is not in the allowlist."""
        self.assertFalse(_is_safe_export_dir(Path("/etc")))

    def test_tilde_expands_correctly_for_documents(self) -> None:
        """~-based path for Documents resolves and is accepted."""
        self.assertTrue(
            _is_safe_export_dir(Path("~/Documents")),
            "~/Documents (unexpanded) must be accepted after expanduser",
        )

    def test_downloads_subpath_allowed(self) -> None:
        """~/Downloads/subdir is in the allowlist."""
        subpath = Path.home() / "Downloads" / "krab_exports" / "batch1"
        self.assertTrue(_is_safe_export_dir(subpath))

    def test_home_root_rejected(self) -> None:
        """Home directory root ~ is NOT in the allowlist (too broad)."""
        self.assertFalse(
            _is_safe_export_dir(Path.home()),
            "~ (home root) must be rejected as it is not in _EXPORT_ALLOWED_ROOTS",
        )


# ---------------------------------------------------------------------------
# handle_export_obsidian integration smoke tests using _is_safe_export_dir logic
# ---------------------------------------------------------------------------

class ExportObsidianAllowlistIntegrationW1432TestCase(unittest.TestCase):
    """Integration smoke tests: handlers reject disallowed output_dir values."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.svc = _make_service(self.data_dir)
        self.item_id = _add_item(self.svc)

    def test_handle_export_obsidian_rejects_ssh(self) -> None:
        """handle_export_obsidian raises when output_dir targets ~/.ssh."""
        ssh = str(Path.home() / ".ssh")
        # Both ValueError (from _resolve_export_dir) and RuntimeError are acceptable
        with self.assertRaises((ValueError, RuntimeError)):
            self.svc.handle_export_obsidian(
                {"ids": [self.item_id], "output_dir": ssh}
            )

    def test_handle_batch_export_rejects_traversal(self) -> None:
        """handle_batch_export raises when output_dir traverses outside allowed roots."""
        traversal = str(Path.home() / "Documents" / "../../../../etc")
        with self.assertRaises((ValueError, RuntimeError)):
            self.svc.handle_batch_export(
                {"output_dir": traversal, "formats": ["csv"]}
            )

    def test_handle_export_obsidian_allows_tmp(self) -> None:
        """handle_export_obsidian accepts /tmp as output_dir."""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp_out:
            result = self.svc.handle_export_obsidian(
                {"ids": [self.item_id], "output_dir": tmp_out}
            )
        self.assertIn("file", result)


if __name__ == "__main__":
    unittest.main()
