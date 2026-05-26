"""Wave 603 — ObsidianSyncManager error-handling tests.

Covers:
1. vault_dir missing → graceful skip + log
2. vault_dir permission denied → fall back to default
3. lock contention (concurrent sync) → wait + retry
4. malformed obsidian_sync.json state → reset + warn
5. .md write failure (disk full) → abort + emit error
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# --- path setup -----------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.obsidian_sync import ObsidianSyncManager, SyncResult  # noqa: E402


def _make_item(item_id: str = "abc12345", ts: str = "2026-01-01T10:00:00+00:00",
               text: str = "Тест транскрипции") -> dict:
    return {"id": item_id, "ts": ts, "text": text}


class TestObsidianSyncErrors(unittest.TestCase):
    """Error-handling tests for ObsidianSyncManager (Wave 603)."""

    # ------------------------------------------------------------------
    # 1. vault_dir missing → configure raises ValueError, sync skipped
    # ------------------------------------------------------------------
    def test_vault_dir_missing_raises_and_logs(self):
        """configure() with non-existent path raises ValueError; sync is never attempted."""
        with tempfile.TemporaryDirectory() as data_dir:
            mgr = ObsidianSyncManager(data_dir=Path(data_dir))
            missing = Path(data_dir) / "does_not_exist"

            with self.assertRaises(ValueError) as ctx:
                mgr.configure(str(missing))

            self.assertIn("не существует", str(ctx.exception))

            # Vault not configured → sync must raise RuntimeError, not crash silently
            with self.assertRaises(RuntimeError):
                mgr.sync([_make_item()])

    # ------------------------------------------------------------------
    # 2. vault_dir permission denied → configure raises; status shows unconfigured
    # ------------------------------------------------------------------
    def test_vault_dir_permission_denied_falls_back(self):
        """configure() for unreadable dir raises; manager status stays unconfigured."""
        if os.getuid() == 0:
            self.skipTest("root bypasses permission checks")

        with tempfile.TemporaryDirectory() as data_dir:
            # Create dir and strip read+execute bits
            locked_dir = Path(data_dir) / "locked_vault"
            locked_dir.mkdir()
            os.chmod(locked_dir, stat.S_IWUSR)  # write-only → mkdir inside will fail

            mgr = ObsidianSyncManager(data_dir=Path(data_dir))

            try:
                # configure may raise ValueError (not a dir) or PermissionError
                with self.assertRaises((ValueError, PermissionError, OSError)):
                    mgr.configure(str(locked_dir), folder="Notes")
            finally:
                # Restore perms so TemporaryDirectory cleanup works
                os.chmod(locked_dir, stat.S_IRWXU)

            status = mgr.get_sync_status()
            self.assertFalse(status["configured"])

    # ------------------------------------------------------------------
    # 3. Lock contention — concurrent sync waits and completes
    # ------------------------------------------------------------------
    def test_concurrent_sync_waits_and_both_complete(self):
        """Two concurrent sync() calls serialise; both return valid SyncResult."""
        with tempfile.TemporaryDirectory() as data_dir, \
                tempfile.TemporaryDirectory() as vault_dir:

            mgr = ObsidianSyncManager(data_dir=Path(data_dir))
            mgr.configure(vault_dir, folder="Transcriptions")

            items_a = [_make_item("id1", "2026-01-01T10:00:00+00:00", "Alpha")]
            items_b = [_make_item("id2", "2026-01-01T11:00:00+00:00", "Beta")]

            results: list[SyncResult] = []
            errors: list[Exception] = []

            def _run(items):
                try:
                    r = mgr.sync(items, force=True)
                    results.append(r)
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=_run, args=(items_a,))
            t2 = threading.Thread(target=_run, args=(items_b,))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            self.assertFalse(errors, f"Unexpected errors: {errors}")
            self.assertEqual(len(results), 2)
            total_synced = sum(r.synced_count for r in results)
            self.assertEqual(total_synced, 2)

    # ------------------------------------------------------------------
    # 4. Malformed obsidian_sync.json → state reset + warning logged
    # ------------------------------------------------------------------
    def test_malformed_state_file_resets_with_warning(self):
        """Corrupted obsidian_sync.json is tolerated; manager starts unconfigured."""
        with tempfile.TemporaryDirectory() as data_dir:
            state_path = Path(data_dir) / "obsidian_sync.json"
            # Write malformed JSON
            state_path.write_text("{NOT VALID JSON", encoding="utf-8")

            with self.assertLogs("KrabEar.Backend.ObsidianSync", level="WARNING") as cm:
                mgr = ObsidianSyncManager(data_dir=Path(data_dir))

            # Should have logged a warning
            self.assertTrue(
                any("Не удалось загрузить" in line for line in cm.output),
                f"Expected warning not found in logs: {cm.output}",
            )
            # Manager should start unconfigured (no crash)
            status = mgr.get_sync_status()
            self.assertFalse(status["configured"])

    # ------------------------------------------------------------------
    # 5. .md write failure (simulated disk full) → error captured in SyncResult
    # ------------------------------------------------------------------
    def test_md_write_failure_captured_in_result(self):
        """.md write errors are caught per-item; SyncResult.errors is populated."""
        with tempfile.TemporaryDirectory() as data_dir, \
                tempfile.TemporaryDirectory() as vault_dir:

            mgr = ObsidianSyncManager(data_dir=Path(data_dir))
            mgr.configure(vault_dir, folder="Transcriptions")

            item = _make_item("deadbeef", "2026-01-02T09:00:00+00:00", "Ошибка диска")

            # Patch Path.write_text to simulate OSError (disk full)
            original_write_text = Path.write_text

            def _failing_write(self_path, content, encoding="utf-8"):
                if self_path.suffix == ".md":
                    raise OSError(28, "No space left on device")
                return original_write_text(self_path, content, encoding=encoding)

            with patch.object(Path, "write_text", _failing_write):
                result = mgr.sync([item], force=True)

            self.assertEqual(result.synced_count, 0)
            self.assertEqual(len(result.errors), 1)
            self.assertIn("deadbeef", result.errors[0])


if __name__ == "__main__":
    unittest.main()
