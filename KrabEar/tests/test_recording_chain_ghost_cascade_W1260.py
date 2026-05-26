"""Tests for ghost item_id cascade cleanup in RecordingChainManager (W1253 RC-3).

Covers:
  - remove_item_from_all_chains purges references from all chains
  - delete_history_item cascades to chains via HistoryService
  - archive_items cascades to chains via ArchiveManager
  - remove_item_from_all_chains is a no-op when item not in any chain
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_chain import RecordingChainManager  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str, text: str = "hello", ts: str = "2020-01-01T00:00:00+00:00") -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.duration_sec = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "duration_sec": self.duration_sec}


class FakeStore:
    """Minimal store fake used across multiple test cases."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: list[str] = []
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()

    def add_item(self, item_id: str, text: str = "hello", ts: str = "2020-01-01T00:00:00+00:00") -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text, ts)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            del self._items[item_id]
            self._deleted.append(item_id)
            return True
        return False

    def _lock(self):
        return self._lock_obj

    def _load_active_items_unlocked(self) -> list[FakeHistoryItem]:
        return list(self._items.values())

    def _append_ndjson(self, path: Any, payload: dict) -> None:
        self._tombstones.append(payload)

    @property
    def tombstones_path(self) -> str:
        return "fake_tombstones.ndjson"


# ---------------------------------------------------------------------------
# Tests for RecordingChainManager.remove_item_from_all_chains
# ---------------------------------------------------------------------------

class RemoveItemFromAllChainsTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    # ------------------------------------------------------------------
    # Core behaviour
    # ------------------------------------------------------------------

    def test_remove_item_from_all_chains_purges_references(self) -> None:
        """item_id removed from every chain it appears in; chains without it unchanged."""
        cid1 = self._mgr.start_chain("Chain A")
        cid2 = self._mgr.start_chain("Chain B")
        cid3 = self._mgr.start_chain("Chain C")

        self._mgr.add_to_chain(cid1, "item-1")
        self._mgr.add_to_chain(cid1, "item-2")
        self._mgr.add_to_chain(cid2, "item-1")  # same item in second chain
        self._mgr.add_to_chain(cid3, "item-99")  # different item, should be untouched

        count = self._mgr.remove_item_from_all_chains("item-1")

        self.assertEqual(count, 2, "item-1 was in 2 chains, expected removed_from=2")
        # item-1 gone from cid1
        chain1 = self._mgr.get_chain(cid1)
        self.assertNotIn("item-1", chain1["item_ids"])
        self.assertIn("item-2", chain1["item_ids"])
        # item-1 gone from cid2
        chain2 = self._mgr.get_chain(cid2)
        self.assertNotIn("item-1", chain2["item_ids"])
        # item-99 still present in cid3
        chain3 = self._mgr.get_chain(cid3)
        self.assertIn("item-99", chain3["item_ids"])

    def test_remove_item_from_chain_when_not_in_any_chain_no_op(self) -> None:
        """remove_item_from_all_chains returns 0 and doesn't crash when item absent."""
        cid = self._mgr.start_chain("Solo chain")
        self._mgr.add_to_chain(cid, "item-A")

        count = self._mgr.remove_item_from_all_chains("nonexistent-id")

        self.assertEqual(count, 0)
        chain = self._mgr.get_chain(cid)
        self.assertIn("item-A", chain["item_ids"])

    def test_remove_item_from_all_chains_empty_id_no_op(self) -> None:
        """Empty / whitespace item_id is silently ignored, returns 0."""
        cid = self._mgr.start_chain("Solo chain")
        self._mgr.add_to_chain(cid, "item-Z")

        self.assertEqual(self._mgr.remove_item_from_all_chains(""), 0)
        self.assertEqual(self._mgr.remove_item_from_all_chains("   "), 0)

    def test_remove_item_from_all_chains_persisted(self) -> None:
        """After remove, a freshly loaded manager no longer sees the item_id."""
        cid = self._mgr.start_chain("Persist test")
        self._mgr.add_to_chain(cid, "item-X")

        self._mgr.remove_item_from_all_chains("item-X")

        # Reload from disk
        mgr2 = RecordingChainManager(store=self._store)
        chain = mgr2.get_chain(cid)
        self.assertNotIn("item-X", chain["item_ids"])

    # ------------------------------------------------------------------
    # get_chain stub regression: deleted item must not produce {"id":…} stub
    # ------------------------------------------------------------------

    def test_get_chain_no_stubs_after_cascade(self) -> None:
        """After cascade removal, get_chain.items must not contain bare {id:…} stubs."""
        self._store.add_item("item-alive")
        cid = self._mgr.start_chain("No stubs")
        self._mgr.add_to_chain(cid, "item-alive")
        self._mgr.add_to_chain(cid, "item-dead")

        # Simulate item-dead being deleted — cascade purge
        self._mgr.remove_item_from_all_chains("item-dead")

        chain = self._mgr.get_chain(cid)
        # Only item-alive remains in item_ids
        self.assertEqual(chain["item_ids"], ["item-alive"])
        # items list should not have a stub for item-dead
        stub_ids = [i.get("id") for i in chain["items"] if list(i.keys()) == ["id"]]
        self.assertNotIn("item-dead", stub_ids)


# ---------------------------------------------------------------------------
# Integration: HistoryService.handle_delete_history_item cascade
# ---------------------------------------------------------------------------

class HistoryServiceDeleteCascadeTestCase(unittest.TestCase):
    """HistoryService.handle_delete_history_item must cascade to chains when
    _recording_chain_mgr is wired (late-injection)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._store.add_item("item-to-delete")

    def _make_service(self):
        from backend.history_service import HistoryService
        svc = HistoryService(store=self._store)
        return svc

    def test_delete_history_item_cascades_to_chains(self) -> None:
        """Deleting a history item removes it from all chains it belongs to."""
        chain_mgr = RecordingChainManager(store=self._store)
        cid = chain_mgr.start_chain("Meeting")
        chain_mgr.add_to_chain(cid, "item-to-delete")
        chain_mgr.add_to_chain(cid, "item-survivor")

        svc = self._make_service()
        svc._recording_chain_mgr = chain_mgr  # late-injection

        svc.handle_delete_history_item({"id": "item-to-delete"})

        chain = chain_mgr.get_chain(cid)
        self.assertNotIn("item-to-delete", chain["item_ids"])
        self.assertIn("item-survivor", chain["item_ids"])

    def test_delete_history_item_no_chain_mgr_still_works(self) -> None:
        """Without late-injection, delete still succeeds (no AttributeError)."""
        svc = self._make_service()
        # _recording_chain_mgr is None by default
        result = svc.handle_delete_history_item({"id": "item-to-delete"})
        self.assertTrue(result.get("deleted"))


# ---------------------------------------------------------------------------
# Integration: ArchiveManager.archive_items cascade
# ---------------------------------------------------------------------------

class ArchiveManagerCascadeTestCase(unittest.TestCase):
    """ArchiveManager.archive_items must cascade to chains when
    _recording_chain_mgr is wired (late-injection)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._store.add_item("item-alpha")
        self._store.add_item("item-beta")
        self._store.add_item("item-gamma")

    def _make_archive_manager(self):
        from backend.archive_manager import ArchiveManager
        return ArchiveManager(store=self._store)

    def test_archive_items_cascades_to_chains(self) -> None:
        """Archiving items removes them from all chains they belong to."""
        chain_mgr = RecordingChainManager(store=self._store)
        cid = chain_mgr.start_chain("Long meeting")
        chain_mgr.add_to_chain(cid, "item-alpha")
        chain_mgr.add_to_chain(cid, "item-beta")
        chain_mgr.add_to_chain(cid, "item-gamma")

        am = self._make_archive_manager()
        am._recording_chain_mgr = chain_mgr  # late-injection

        result = am.archive_items(["item-alpha", "item-beta"])

        self.assertEqual(result.archived_count, 2)
        chain = chain_mgr.get_chain(cid)
        self.assertNotIn("item-alpha", chain["item_ids"])
        self.assertNotIn("item-beta", chain["item_ids"])
        # item-gamma not archived, still present
        self.assertIn("item-gamma", chain["item_ids"])

    def test_archive_items_no_chain_mgr_still_works(self) -> None:
        """Without late-injection, archive_items still works (no AttributeError)."""
        am = self._make_archive_manager()
        # _recording_chain_mgr is None by default
        result = am.archive_items(["item-alpha"])
        self.assertEqual(result.archived_count, 1)


if __name__ == "__main__":
    unittest.main()
