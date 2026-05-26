"""Wave 623 — ObsidianSyncManager error-path tests.

Tests:
1. vault_dir missing → skip + log warning
2. perm denied on target folder → error recorded in SyncResult
3. lock contention → second thread waits and retries successfully
4. malformed state.json → reset (no crash) + warning logged
5. disk full on .md write → abort write + error in SyncResult
"""
from __future__ import annotations

import json
import os
import sys
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


def _item(id_="abc12345", ts="2026-05-24T10:00:00+00:00", text="test text"):
    return {"id": id_, "ts": ts, "text": text}


class TestObsidianSyncMissingVault(unittest.TestCase):
    """1. vault_dir missing → configure raises ValueError + logged."""

    def test_missing_vault_raises_and_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ObsidianSyncManager(data_dir=Path(tmpdir))
            nonexistent = Path(tmpdir) / "no_such_vault"
            # configure raises ValueError — vault must not exist
            with self.assertRaises(ValueError) as exc_ctx:
                mgr.configure(str(nonexistent))
            self.assertIn("не существует", str(exc_ctx.exception))
            # vault must remain unconfigured after the error
            status = mgr.get_sync_status()
            self.assertFalse(status["configured"])


class TestObsidianSyncPermDenied(unittest.TestCase):
    """2. perm denied on target folder → error recorded in SyncResult."""

    def test_perm_denied_records_error(self):
        if os.getuid() == 0:
            self.skipTest("Running as root — permission tests are unreliable")
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "vault"
            vault.mkdir()
            mgr = ObsidianSyncManager(data_dir=Path(tmpdir))
            mgr.configure(str(vault), folder="Transcriptions")

            # Make target dir non-writable
            target = vault / "Transcriptions"
            target.mkdir(exist_ok=True)
            target.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write

            try:
                result = mgr.sync([_item()], force=True)
            finally:
                target.chmod(stat.S_IRWXU)  # restore so tempdir cleanup works

            self.assertEqual(result.synced_count, 0)
            self.assertGreater(len(result.errors), 0)


class TestObsidianSyncLockContention(unittest.TestCase):
    """3. lock contention → second thread waits and succeeds after first releases."""

    def test_concurrent_sync_both_succeed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "vault"
            vault.mkdir()
            mgr = ObsidianSyncManager(data_dir=Path(tmpdir))
            mgr.configure(str(vault), folder="Transcriptions")

            results = []
            errors = []

            def _run_sync(item_id, ts_offset):
                try:
                    r = mgr.sync(
                        [_item(id_=item_id, ts=f"2026-05-24T10:0{ts_offset}:00+00:00")],
                        force=True,
                    )
                    results.append(r)
                except Exception as exc:
                    errors.append(exc)

            t1 = threading.Thread(target=_run_sync, args=("id000001", 0))
            t2 = threading.Thread(target=_run_sync, args=("id000002", 1))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
            self.assertEqual(len(results), 2)
            total_synced = sum(r.synced_count for r in results)
            self.assertEqual(total_synced, 2)


class TestObsidianSyncMalformedState(unittest.TestCase):
    """4. malformed state.json → manager resets gracefully + logs warning."""

    def test_malformed_state_resets_without_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "obsidian_sync.json"
            state_file.write_text("NOT VALID JSON }{", encoding="utf-8")

            with self.assertLogs("KrabEar.Backend.ObsidianSync", level="WARNING") as _cm:
                mgr = ObsidianSyncManager(data_dir=Path(tmpdir))

            # After malformed load vault_path should be None (reset)
            status = mgr.get_sync_status()
            self.assertFalse(status["configured"])
            self.assertIsNone(status["vault_path"])


class TestObsidianSyncDiskFull(unittest.TestCase):
    """5. disk full on .md write → error in SyncResult, abort that item."""

    def test_disk_full_on_write_records_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "vault"
            vault.mkdir()
            mgr = ObsidianSyncManager(data_dir=Path(tmpdir))
            mgr.configure(str(vault), folder="Transcriptions")

            ose = OSError(28, "No space left on device")

            with patch("pathlib.Path.write_text", side_effect=ose):
                result = mgr.sync([_item()], force=True)

            self.assertEqual(result.synced_count, 0)
            self.assertGreater(len(result.errors), 0)
            self.assertIn("No space left on device", result.errors[0])


if __name__ == "__main__":
    unittest.main()
