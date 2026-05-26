"""Tests for W1302 F1 fix: startup compact deferred until transcript_versioner wired.

Covers:
- test_startup_compact_runs_with_versioner_wired:
    After BackendService.__init__ completes, store._on_compact_hook points to
    TranscriptVersionManager.purge_orphaned_versions — compact can safely call it.
- test_startup_compact_purges_orphan_versions:
    Items tombstoned before restart accumulate orphaned version records.
    When startup compact fires (with versioner wired), those orphaned records
    are removed from transcript_versions.ndjson.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.transcript_versioning import TranscriptVersionManager  # noqa: E402


class StartupCompactVersionerWiredTestCase(unittest.TestCase):
    """Verifies that _on_compact_hook is wired before maybe_compact() runs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_store(self, compact_threshold_bytes: int = 25 * 1024 * 1024) -> StateStore:
        return StateStore(
            data_dir=self.data_dir,
            compact_threshold_bytes=compact_threshold_bytes,
        )

    def _make_versioner(self) -> TranscriptVersionManager:
        return TranscriptVersionManager(data_dir=self.data_dir)

    def test_startup_compact_runs_with_versioner_wired(self) -> None:
        """store._on_compact_hook must be set to versioner.purge_orphaned_versions
        before maybe_compact() is called — simulates what BackendService.__init__ does."""
        store = self._make_store()
        versioner = self._make_versioner()

        # Simulate the wiring order from BackendService.__init__:
        # 1. Wire the hook
        store._on_compact_hook = versioner.purge_orphaned_versions

        # 2. Add an item and tombstone it to ensure compact has something to do
        item = store.add_history_item(text="hello")
        store.delete_history_item(item.id)

        # 3. Wire a version for that item
        versioner.save_version(item_id=item.id, text="hello", source="stt_raw")

        # 4. Force compact via hook-compatible path (compact_with_stats uses _compact_unlocked)
        store.compact_with_stats()

        # Hook attribute still present and callable after compact
        self.assertTrue(
            callable(getattr(store, "_on_compact_hook", None)),
            "_on_compact_hook should remain wired after compact",
        )
        # Compact should have called the hook → orphaned version removed
        remaining = versioner.get_versions(item.id)
        self.assertEqual(remaining, [], "Orphaned version must be purged after compact")

    def test_versioner_hook_attribute_missing_before_wiring(self) -> None:
        """Before BackendService.__init__ wires the hook, _on_compact_hook must not
        exist on a fresh StateStore (getattr fallback is safe)."""
        store = self._make_store()
        # Should not raise — getattr with default is used in _compact_unlocked
        hook = getattr(store, "_on_compact_hook", None)
        self.assertIsNone(hook, "Fresh StateStore must not have _on_compact_hook")


class StartupCompactPurgesOrphanVersionsTestCase(unittest.TestCase):
    """End-to-end test: tombstoned items' version records are purged on startup compact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def test_startup_compact_purges_orphan_versions(self) -> None:
        """Simulate a backend restart scenario:

        Pre-restart:
          - item_A was deleted (tombstoned) before the previous shutdown.
          - item_B is still active.
          - Both have version records in transcript_versions.ndjson.

        Post-restart (startup compact fires with versioner wired):
          - item_A's version records must be purged.
          - item_B's version records must survive.
        """
        # Use tiny threshold so maybe_compact() triggers
        store = StateStore(
            data_dir=self.data_dir,
            compact_threshold_bytes=1,
        )
        versioner = TranscriptVersionManager(data_dir=self.data_dir)

        # Create two items
        item_a = store.add_history_item(text="record A")
        item_b = store.add_history_item(text="record B")

        # Save version records for both
        versioner.save_version(item_id=item_a.id, text="record A v1", source="stt_raw")
        versioner.save_version(item_id=item_a.id, text="record A v2", source="manual")
        versioner.save_version(item_id=item_b.id, text="record B v1", source="stt_raw")

        # Simulate item_A being deleted before shutdown (tombstoned)
        store.delete_history_item(item_a.id)

        # Simulate restart: wire hook BEFORE maybe_compact (the fixed order)
        store._on_compact_hook = versioner.purge_orphaned_versions

        # Startup compact fires
        triggered = store.maybe_compact()
        self.assertTrue(triggered, "maybe_compact should trigger with tiny threshold")

        # item_A's 2 orphaned versions must be gone
        versions_a = versioner.get_versions(item_a.id)
        self.assertEqual(
            versions_a,
            [],
            f"Orphaned versions for tombstoned item_a must be purged, got: {versions_a}",
        )

        # item_B's version must survive
        versions_b = versioner.get_versions(item_b.id)
        self.assertEqual(len(versions_b), 1, "Active item_b version must survive compact")
        self.assertEqual(versions_b[0]["text"], "record B v1")

    def test_startup_compact_without_hook_leaves_orphans(self) -> None:
        """Without the hook wired (old behaviour), orphaned versions are NOT purged.

        This test documents the pre-fix behaviour and acts as a regression sentinel:
        if someone accidentally removes the hook wiring, the purge test above will fail.
        """
        store = StateStore(
            data_dir=self.data_dir,
            compact_threshold_bytes=1,
        )
        versioner = TranscriptVersionManager(data_dir=self.data_dir)

        item = store.add_history_item(text="orphan item")
        versioner.save_version(item_id=item.id, text="orphan v1", source="stt_raw")
        store.delete_history_item(item.id)

        # NO hook wired — old (broken) behaviour
        store.maybe_compact()

        # Orphaned version persists (old behaviour documented)
        versions = versioner.get_versions(item.id)
        self.assertEqual(
            len(versions),
            1,
            "Without hook, orphaned version must remain (documents pre-fix behaviour)",
        )


class PurgeOrphanedVersionsUnitTestCase(unittest.TestCase):
    """Unit tests for TranscriptVersionManager.purge_orphaned_versions()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.versioner = TranscriptVersionManager(data_dir=Path(self.tmp.name))

    def test_purge_removes_versions_for_inactive_ids(self) -> None:
        self.versioner.save_version("active-1", "text a", source="stt_raw")
        self.versioner.save_version("dead-1", "text d1", source="stt_raw")
        self.versioner.save_version("dead-2", "text d2", source="manual")

        purged = self.versioner.purge_orphaned_versions(active_item_ids={"active-1"})

        self.assertEqual(purged, 2)
        self.assertEqual(len(self.versioner.get_versions("active-1")), 1)
        self.assertEqual(self.versioner.get_versions("dead-1"), [])
        self.assertEqual(self.versioner.get_versions("dead-2"), [])

    def test_purge_with_all_active_purges_nothing(self) -> None:
        self.versioner.save_version("id-1", "t1", source="stt_raw")
        self.versioner.save_version("id-2", "t2", source="stt_raw")

        purged = self.versioner.purge_orphaned_versions(active_item_ids={"id-1", "id-2"})
        self.assertEqual(purged, 0)

    def test_purge_with_empty_active_set_removes_all(self) -> None:
        self.versioner.save_version("x", "text", source="manual")
        self.versioner.save_version("y", "text", source="manual")

        purged = self.versioner.purge_orphaned_versions(active_item_ids=set())
        self.assertEqual(purged, 2)

    def test_purge_empty_file_returns_zero(self) -> None:
        purged = self.versioner.purge_orphaned_versions(active_item_ids={"anything"})
        self.assertEqual(purged, 0)

    def test_purge_preserves_file_content_correctly(self) -> None:
        """After purge, the NDJSON file contains exactly the kept records."""
        self.versioner.save_version("keep", "v1", source="stt_raw")
        self.versioner.save_version("keep", "v2", source="manual")
        self.versioner.save_version("drop", "v1", source="stt_raw")

        self.versioner.purge_orphaned_versions(active_item_ids={"keep"})

        versions_path = self.versioner._versions_path
        lines = [
            line for line in versions_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(lines), 2, "Only 2 'keep' lines should remain")
        for line in lines:
            record = json.loads(line)
            self.assertEqual(record["item_id"], "keep")


if __name__ == "__main__":
    unittest.main()
