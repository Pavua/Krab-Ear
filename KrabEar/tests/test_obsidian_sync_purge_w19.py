"""Tests for wave-19 obsidian_sync.py fixes:

FINDING 1 (LOW, silent purge failure):
  purge_all_synced_files() must surface per-file unlink failures instead of
  swallowing them.  If any .md cannot be deleted, OSError must be raised after
  the loop (so the caller's try/except records it as a secondary error) and the
  successfully-deleted files must already be gone.

FINDING 2 (LOW, confused-deputy):
  configure() must emit a WARNING log whenever vault_path is set/changed so the
  write target is auditable.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TESTS_DIR.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# KrabEar package root
_KRAB_EAR_ROOT = _PROJECT_ROOT / "KrabEar"
if str(_KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(_KRAB_EAR_ROOT))

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


class TestPurgePartialFailureSurfaced(unittest.TestCase):
    """W19 Fix 1: partial unlink failure must raise OSError, not be swallowed."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmpdir.name) / "vault"
        self.vault_dir.mkdir()
        self.data_dir = Path(self._tmpdir.name) / "data"
        self.data_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)
        self.mgr.configure(str(self.vault_dir))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_md(self, name: str) -> Path:
        """Create a dummy .md file in the sync folder."""
        p = self.vault_dir / "Transcriptions" / name
        p.write_text("PII content", encoding="utf-8")
        return p

    def test_raise_when_one_unlink_fails(self) -> None:
        """When one unlink raises OSError, purge_all_synced_files must raise."""
        good = self._make_md("transcript_good.md")
        bad = self._make_md("transcript_bad.md")

        # Use the real unlink for 'bad' path comparison, but raise for it.
        # Avoid patching Path.unlink globally (causes recursion); instead
        # patch at the instance level via side_effect on glob results.
        original_unlink = Path.unlink

        def _selective_unlink(path, missing_ok=False):
            if path.name == bad.name:
                raise OSError(13, "Permission denied", str(bad))
            original_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", _selective_unlink):
            with self.assertRaises(OSError) as ctx:
                self.mgr.purge_all_synced_files()

        err_msg = str(ctx.exception)
        self.assertIn(str(bad), err_msg, "Error message should contain the failed path")
        # The good file must have been deleted despite the failure
        self.assertFalse(good.exists(), "Successfully unlinkable file must be gone")

    def test_raise_message_contains_failed_path(self) -> None:
        """OSError message must name the path(s) that could not be deleted."""
        failing = self._make_md("transcript_fail.md")

        def _always_fail(path, missing_ok=False):
            raise OSError(1, "op not permitted", str(path))

        with patch.object(Path, "unlink", _always_fail):
            with self.assertRaises(OSError) as ctx:
                self.mgr.purge_all_synced_files()

        self.assertIn(str(failing), str(ctx.exception))

    def test_no_raise_when_all_succeed(self) -> None:
        """Normal case: no exception, returns count of deleted files."""
        self._make_md("transcript_a.md")
        self._make_md("transcript_b.md")

        result = self.mgr.purge_all_synced_files()
        self.assertEqual(result, 2)

    def test_state_reset_even_on_partial_failure(self) -> None:
        """last_sync_ts must be cleared even when some files fail to delete."""
        self._make_md("transcript_x.md")
        # Manually set last_sync_ts
        self.mgr._last_sync_ts = "2026-01-01T00:00:00+00:00"

        def _fail(path, missing_ok=False):
            raise OSError(1, "error")

        with patch.object(Path, "unlink", _fail):
            try:
                self.mgr.purge_all_synced_files()
            except OSError:
                pass

        self.assertIsNone(
            self.mgr._last_sync_ts,
            "last_sync_ts must be reset even on partial purge failure",
        )

    def test_warning_logged_per_failed_file(self) -> None:
        """Each failed unlink must produce a WARNING log entry."""
        self._make_md("transcript_warn.md")

        def _fail(path, missing_ok=False):
            raise OSError(1, "error")

        with self.assertLogs("KrabEar.Backend.ObsidianSync", level=logging.WARNING) as cm:
            with patch.object(Path, "unlink", _fail):
                try:
                    self.mgr.purge_all_synced_files()
                except OSError:
                    pass

        # At least one WARNING about the failed path
        warnings = [r for r in cm.output if "не удалось удалить" in r or "WARNING" in r]
        self.assertTrue(len(warnings) >= 1, "Expected at least one WARNING for failed unlink")

    def test_no_files_is_noop(self) -> None:
        """purge_all_synced_files with no .md files returns 0 without raising."""
        result = self.mgr.purge_all_synced_files()
        self.assertEqual(result, 0)

    def test_vault_not_configured_returns_zero(self) -> None:
        """No vault configured → no-op, returns 0."""
        mgr = ObsidianSyncManager()
        result = mgr.purge_all_synced_files()
        self.assertEqual(result, 0)


class TestConfigureVaultPathAuditLog(unittest.TestCase):
    """W19 Fix 2: configure() must emit a WARNING about the vault_path."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmpdir.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_configure_emits_warning(self) -> None:
        """configure() must log a WARNING containing the resolved vault path."""
        with self.assertLogs("KrabEar.Backend.ObsidianSync", level=logging.WARNING) as cm:
            self.mgr.configure(str(self.vault_dir))

        combined = "\n".join(cm.output)
        self.assertIn(str(self.vault_dir.resolve()), combined,
                      "WARNING must include the resolved vault path")

    def test_configure_warning_includes_folder(self) -> None:
        """WARNING log must also mention the folder name."""
        with self.assertLogs("KrabEar.Backend.ObsidianSync", level=logging.WARNING) as cm:
            self.mgr.configure(str(self.vault_dir), folder="MyTranscripts")

        combined = "\n".join(cm.output)
        self.assertIn("MyTranscripts", combined,
                      "WARNING must include the folder name")

    def test_configure_still_returns_correct_dict(self) -> None:
        """After adding the WARNING, configure() must still return the expected dict."""
        with self.assertLogs("KrabEar.Backend.ObsidianSync", level=logging.WARNING):
            result = self.mgr.configure(str(self.vault_dir), folder="Krab")

        self.assertIn("vault_path", result)
        self.assertIn("folder", result)
        self.assertIn("folder_full_path", result)
        self.assertEqual(result["folder"], "Krab")


if __name__ == "__main__":
    unittest.main()
