"""Wave 659 — ObsidianSyncManager error-path tests (5 scenarios)."""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.obsidian_sync import ObsidianSyncManager


def _item(ts="2025-01-01T10:00:00+00:00", item_id="abc12345", text="hello"):
    return {"id": item_id, "ts": ts, "text": text}


class TestVaultDirMissing(unittest.TestCase):
    """vault_dir missing → skip and log a warning (no raise)."""

    def test_missing_vault_raises_value_error_on_configure(self):
        with tempfile.TemporaryDirectory() as data_dir:
            mgr = ObsidianSyncManager(data_dir=Path(data_dir))
            with self.assertRaises(ValueError):
                mgr.configure("/nonexistent/vault/path/xyz123")

    def test_sync_without_configure_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            mgr = ObsidianSyncManager(data_dir=Path(data_dir))
            with self.assertRaises(RuntimeError):
                mgr.sync([_item()])


class TestPermDenied(unittest.TestCase):
    """Permission denied on target dir → error captured in SyncResult, no crash."""

    def test_perm_denied_write_captured_in_errors(self):
        with tempfile.TemporaryDirectory() as vault_dir:
            with tempfile.TemporaryDirectory() as data_dir:
                mgr = ObsidianSyncManager(data_dir=Path(data_dir))
                mgr.configure(vault_dir)
                # Make the target folder read-only
                target = Path(vault_dir) / "Transcriptions"
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, stat.S_IRUSR | stat.S_IXUSR)
                try:
                    result = mgr.sync([_item()])
                    self.assertEqual(len(result.errors), 1)
                    self.assertEqual(result.synced_count, 0)
                finally:
                    os.chmod(target, stat.S_IRWXU)


class TestLockContention(unittest.TestCase):
    """Lock contention: second sync waits and completes after first releases."""

    def test_concurrent_sync_both_complete(self):
        with tempfile.TemporaryDirectory() as vault_dir:
            with tempfile.TemporaryDirectory() as data_dir:
                mgr = ObsidianSyncManager(data_dir=Path(data_dir))
                mgr.configure(vault_dir)

                results = []
                errors = []

                def run_sync(items):
                    try:
                        r = mgr.sync(items, force=True)
                        results.append(r)
                    except Exception as exc:
                        errors.append(exc)

                items_a = [_item(item_id="aaaa0001", ts="2025-01-01T10:00:00+00:00")]
                items_b = [_item(item_id="bbbb0002", ts="2025-01-02T10:00:00+00:00")]

                t1 = threading.Thread(target=run_sync, args=(items_a,))
                t2 = threading.Thread(target=run_sync, args=(items_b,))
                t1.start(); t2.start()
                t1.join(timeout=10); t2.join(timeout=10)

                self.assertFalse(errors, f"Unexpected errors: {errors}")
                self.assertEqual(len(results), 2)
                total_synced = sum(r.synced_count for r in results)
                self.assertEqual(total_synced, 2)


class TestMalformedStateJson(unittest.TestCase):
    """Malformed state.json → reset with warning, manager still usable."""

    def test_malformed_state_resets_and_warns(self):
        with tempfile.TemporaryDirectory() as data_dir:
            state_path = Path(data_dir) / "obsidian_sync.json"
            state_path.write_text("{invalid json!!!", encoding="utf-8")

            import logging
            with self.assertLogs("KrabEar.Backend.ObsidianSync", level=logging.WARNING):
                mgr = ObsidianSyncManager(data_dir=Path(data_dir))

            # After malformed load, vault must not be set
            self.assertIsNone(mgr._vault_path)

    def test_malformed_state_manager_still_configurable(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with tempfile.TemporaryDirectory() as vault_dir:
                state_path = Path(data_dir) / "obsidian_sync.json"
                state_path.write_text("null", encoding="utf-8")

                mgr = ObsidianSyncManager(data_dir=Path(data_dir))
                result = mgr.configure(vault_dir)
                self.assertEqual(result["vault_path"], str(Path(vault_dir).resolve()))


class TestDiskFull(unittest.TestCase):
    """Disk full on write → error recorded in SyncResult, abort gracefully."""

    def test_disk_full_captured_in_errors(self):
        with tempfile.TemporaryDirectory() as vault_dir:
            with tempfile.TemporaryDirectory() as data_dir:
                mgr = ObsidianSyncManager(data_dir=Path(data_dir))
                mgr.configure(vault_dir)

                import builtins
                real_open = builtins.open

                def fake_open(path, mode="r", **kwargs):
                    path_str = str(path)
                    if path_str.endswith(".tmp"):
                        raise OSError(28, "No space left on device")
                    return real_open(path, mode, **kwargs)

                with patch("builtins.open", side_effect=fake_open):
                    # write_text uses pathlib which may not go through builtins.open;
                    # patch Path.write_text directly
                    pass

                # Patch Path.write_text to simulate ENOSPC
                original_write_text = Path.write_text

                def enospc_write_text(self_path, *args, **kwargs):
                    if self_path.suffix == ".md":
                        raise OSError(28, "No space left on device")
                    return original_write_text(self_path, *args, **kwargs)

                with patch.object(Path, "write_text", enospc_write_text):
                    result = mgr.sync([_item()])

                self.assertEqual(result.synced_count, 0)
                self.assertEqual(len(result.errors), 1)
                self.assertIn("No space left", result.errors[0])


if __name__ == "__main__":
    unittest.main()
