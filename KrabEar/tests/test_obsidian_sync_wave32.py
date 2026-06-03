"""wave-32 security regression tests for ObsidianSyncManager.

Covers:
  A1 (MED) — configure() rejects vault_path outside $HOME and inside forbidden subdirs
  A2 (MED) — run_obsidian_sync is in HEAVY_METHODS (throttle); MAX_SYNC_ITEMS cap
  A3 (LOW) — purge_all_synced_files() deletes obsidian_sync.json
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.obsidian_sync import ObsidianSyncManager, MAX_SYNC_ITEMS  # noqa: E402
from backend.ipc_throttle import HEAVY_METHODS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(text: str = "test", ts: str | None = None, item_id: str = "aaa00001") -> dict:
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return {"id": item_id, "ts": ts, "text": text,
            "translated_text": "", "translation_mode": "off",
            "source_lang": "ru", "target_lang": "", "tags": [],
            "diarization": None, "confidence": None}


# ---------------------------------------------------------------------------
# A1 — vault_path validation (home-dir + forbidden subdir)
#      The guard lives in handle_configure() (IPC entrypoint) not configure().
# ---------------------------------------------------------------------------

class TestVaultPathValidation(unittest.TestCase):
    """handle_configure() must reject vault_path outside $HOME and forbidden subdirs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name) / "data"
        self._data_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mgr(self) -> ObsidianSyncManager:
        return ObsidianSyncManager(data_dir=self._data_dir)

    def test_valid_path_under_home(self) -> None:
        """A real directory under $HOME must be accepted by handle_configure()."""
        home = Path.home()
        with tempfile.TemporaryDirectory(dir=home) as vault_dir:
            mgr = self._mgr()
            result = mgr.handle_configure({"vault_path": vault_dir})
            self.assertIn("vault_path", result)
            self.assertTrue(result["vault_path"].startswith(str(home)))

    def test_path_outside_home_rejected_via_ipc(self) -> None:
        """handle_configure() must reject vault_path outside $HOME."""
        mgr = self._mgr()
        with self.assertRaises(ValueError) as cm:
            mgr.handle_configure({"vault_path": "/tmp"})
        self.assertIn("home directory", str(cm.exception))

    def test_ssh_dir_rejected_via_ipc(self) -> None:
        """handle_configure() must reject vault_path inside ~/.ssh."""
        ssh_dir = Path.home() / ".ssh"
        if not ssh_dir.exists():
            self.skipTest("~/.ssh does not exist on this machine")
        mgr = self._mgr()
        with self.assertRaises(ValueError) as cm:
            mgr.handle_configure({"vault_path": str(ssh_dir)})
        self.assertIn("restricted directory", str(cm.exception))

    def test_gnupg_dir_rejected_via_ipc(self) -> None:
        """handle_configure() must reject vault_path inside ~/.gnupg."""
        gnupg_dir = Path.home() / ".gnupg"
        if not gnupg_dir.exists():
            self.skipTest("~/.gnupg does not exist on this machine")
        mgr = self._mgr()
        with self.assertRaises(ValueError) as cm:
            mgr.handle_configure({"vault_path": str(gnupg_dir)})
        self.assertIn("restricted directory", str(cm.exception))

    def test_nonexistent_path_rejected_via_ipc(self) -> None:
        """handle_configure() must reject vault_path that does not exist."""
        mgr = self._mgr()
        # /tmp is outside $HOME so it hits the home-dir check first.
        with self.assertRaises(ValueError):
            mgr.handle_configure({"vault_path": "/this/path/does/not/exist/ever"})

    def test_missing_vault_path_param_rejected(self) -> None:
        """handle_configure() must raise ValueError when vault_path param is absent."""
        mgr = self._mgr()
        with self.assertRaises(ValueError):
            mgr.handle_configure({})

    def test_configure_still_works_with_tmp_dir(self) -> None:
        """configure() (non-IPC path) must still accept /tmp dirs for tests."""
        # This verifies the guard is at the IPC boundary only, not in configure().
        with tempfile.TemporaryDirectory() as vault_dir:
            mgr = self._mgr()
            # Should NOT raise — configure() has no home-dir check.
            result = mgr.configure(vault_dir)
            self.assertIn("vault_path", result)


# ---------------------------------------------------------------------------
# A2 — throttle: run_obsidian_sync in HEAVY_METHODS
# ---------------------------------------------------------------------------

class TestObsidianSyncThrottle(unittest.TestCase):
    """run_obsidian_sync must be in the HEAVY_METHODS throttle bucket."""

    def test_run_obsidian_sync_in_heavy_methods(self) -> None:
        self.assertIn(
            "run_obsidian_sync",
            HEAVY_METHODS,
            "run_obsidian_sync must be in HEAVY_METHODS to rate-limit disk I/O",
        )


# ---------------------------------------------------------------------------
# A2 — MAX_SYNC_ITEMS cap
# ---------------------------------------------------------------------------

class TestMaxSyncItemsCap(unittest.TestCase):
    """sync() must truncate items to MAX_SYNC_ITEMS when the list is too large."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._vault_dir = Path(self._tmp.name) / "vault"
        self._vault_dir.mkdir()
        self._data_dir = Path(self._tmp.name) / "data"
        self._data_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mgr(self) -> ObsidianSyncManager:
        return ObsidianSyncManager(data_dir=self._data_dir)

    def test_max_sync_items_constant_defined(self) -> None:
        """MAX_SYNC_ITEMS must be defined and have a sane value."""
        self.assertIsInstance(MAX_SYNC_ITEMS, int)
        self.assertGreater(MAX_SYNC_ITEMS, 0)
        self.assertLessEqual(MAX_SYNC_ITEMS, 100_000)

    def test_oversized_list_truncated(self) -> None:
        """sync() must write at most MAX_SYNC_ITEMS files even if more are supplied."""
        home = Path.home()
        with tempfile.TemporaryDirectory(dir=home) as vault_dir:
            mgr = self._mgr()
            mgr.configure(vault_dir)

            # Build MAX_SYNC_ITEMS + 50 items, all with distinct timestamps.
            n = MAX_SYNC_ITEMS + 50
            items = [_make_item(
                text=f"item {i}",
                ts=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
                item_id=f"id{i:08d}",
            ) for i in range(n)]

            result = mgr.sync(items, force=True)

            # At most MAX_SYNC_ITEMS files should have been written.
            self.assertLessEqual(
                result.synced_count,
                MAX_SYNC_ITEMS,
                f"sync wrote {result.synced_count} files but MAX_SYNC_ITEMS={MAX_SYNC_ITEMS}",
            )
            self.assertEqual(result.synced_count, MAX_SYNC_ITEMS)

    def test_list_within_cap_not_truncated(self) -> None:
        """sync() must NOT truncate when the list is within the cap."""
        home = Path.home()
        with tempfile.TemporaryDirectory(dir=home) as vault_dir:
            mgr = self._mgr()
            mgr.configure(vault_dir)
            items = [_make_item(
                text=f"item {i}",
                ts=f"2026-02-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
                item_id=f"idb{i:08d}",
            ) for i in range(5)]
            result = mgr.sync(items, force=True)
            self.assertEqual(result.synced_count, 5)


# ---------------------------------------------------------------------------
# A3 — purge deletes obsidian_sync.json
# ---------------------------------------------------------------------------

class TestPurgeDeletesStateFile(unittest.TestCase):
    """purge_all_synced_files() must delete obsidian_sync.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name) / "data"
        self._data_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_purge_deletes_obsidian_sync_json(self) -> None:
        """After purge_all_synced_files(), obsidian_sync.json must not exist."""
        home = Path.home()
        with tempfile.TemporaryDirectory(dir=home) as vault_dir:
            mgr = ObsidianSyncManager(data_dir=self._data_dir)
            mgr.configure(vault_dir)

            # Write at least one item so the state file exists.
            item = _make_item()
            mgr.sync([item], force=True)

            state_path = self._data_dir / "obsidian_sync.json"
            self.assertTrue(state_path.exists(), "obsidian_sync.json should exist after sync")

            # Now purge.
            mgr.purge_all_synced_files()

            self.assertFalse(
                state_path.exists(),
                "obsidian_sync.json must be deleted by purge_all_synced_files()",
            )

    def test_purge_keeps_in_memory_vault_path_but_deletes_json(self) -> None:
        """After purge, vault_path is still configured in memory (session continues)
        but obsidian_sync.json is gone (does not survive restart)."""
        home = Path.home()
        with tempfile.TemporaryDirectory(dir=home) as vault_dir:
            mgr = ObsidianSyncManager(data_dir=self._data_dir)
            mgr.configure(vault_dir)
            item = _make_item()
            mgr.sync([item], force=True)

            state_path = self._data_dir / "obsidian_sync.json"
            self.assertTrue(state_path.exists(), "state file should exist after sync")

            mgr.purge_all_synced_files()

            # JSON file must be gone (won't survive restart).
            self.assertFalse(
                state_path.exists(),
                "obsidian_sync.json must be deleted by purge",
            )
            # In-memory state is still present so the current session can continue.
            status = mgr.get_sync_status()
            self.assertTrue(status["configured"], "vault should still be configured in memory")

    def test_purge_no_vault_configured_is_noop(self) -> None:
        """purge_all_synced_files() with no vault configured must return 0."""
        mgr = ObsidianSyncManager(data_dir=self._data_dir)
        result = mgr.purge_all_synced_files()
        self.assertEqual(result, 0)

    def test_purge_deletes_md_files(self) -> None:
        """purge_all_synced_files() must also delete the .md transcript files."""
        home = Path.home()
        with tempfile.TemporaryDirectory(dir=home) as vault_dir:
            mgr = ObsidianSyncManager(data_dir=self._data_dir)
            mgr.configure(vault_dir)

            # Use distinct second-resolution timestamps so each item gets a
            # unique filename (the sync engine uses HH-MM-SS in the name).
            items = [_make_item(
                item_id=f"purgeid{i:02d}",
                ts=f"2026-01-01T00:00:{i:02d}+00:00",
            ) for i in range(3)]
            result = mgr.sync(items, force=True)
            self.assertEqual(result.synced_count, 3)

            deleted = mgr.purge_all_synced_files()
            self.assertEqual(deleted, 3)

            # Verify no .md files remain in vault.
            target_dir = Path(vault_dir) / "Transcriptions"
            md_files = list(target_dir.glob("*.md")) if target_dir.exists() else []
            self.assertEqual(md_files, [], f"Expected no .md files after purge, found {md_files}")


if __name__ == "__main__":
    unittest.main()
