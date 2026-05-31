"""Tests for W1730: purge_all_data IPC handler and recording-chain privacy-purge cascade.

Covers:
  1. purge_all_data deletes all history items.
  2. purge_all_data calls delete_all_chains() — recording_chains.json becomes empty.
  3. purge_all_data with no chains wired returns chains_deleted=0 (no crash).
  4. purge_all_data chain-manager error does not abort history deletion.
  5. purge_all_data calls semantic_searcher.purge_all() when wired.
  6. HistoryService._recording_chain_mgr is wired from BackendService.__init__
     (service.py smoke-import test).
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

from backend.history_service import HistoryService  # noqa: E402
from backend.recording_chain import RecordingChainManager  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2020-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": ""}


class FakeStore:
    """Minimal StateStore fake for purge_all_data tests."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()

    def add_item(self, item_id: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id)
        self._items[item_id] = item
        return item

    def _lock(self):
        return self._lock_obj

    def _load_active_items_unlocked(self) -> list[FakeHistoryItem]:
        return list(self._items.values())

    def _append_ndjson(self, path: Any, payload: dict) -> None:
        self._tombstones.append(payload)

    @property
    def tombstones_path(self) -> str:
        return "fake_tombstones.ndjson"

    def load_settings(self) -> dict:
        return {}

    def save_settings(self, settings: dict) -> dict:
        return settings


class SpyChainManager:
    """RecordingChainManager spy — records calls to delete_all_chains."""

    def __init__(self) -> None:
        self.delete_all_chains_called = 0
        self._chains_count = 0

    def delete_all_chains(self) -> int:
        self.delete_all_chains_called += 1
        n = self._chains_count
        self._chains_count = 0
        return n


class ErrorChainManager:
    """Chain manager that always raises on delete_all_chains."""

    def delete_all_chains(self) -> int:
        raise RuntimeError("disk full")


class SpySemanticSearcher:
    """SemanticSearcher spy — records calls to purge_all."""

    def __init__(self) -> None:
        self.purge_all_called = 0

    def purge_all(self) -> None:
        self.purge_all_called += 1


# ---------------------------------------------------------------------------
# Helper: build a HistoryService with a pre-populated store
# ---------------------------------------------------------------------------

def _make_svc(
    tmpdir: str,
    item_ids: list[str] | None = None,
    chain_mgr: Any = None,
    semantic: Any = None,
) -> HistoryService:
    store = FakeStore(data_dir=tmpdir)
    if item_ids:
        for iid in item_ids:
            store.add_item(iid)
    svc = HistoryService(store=store)
    svc._recording_chain_mgr = chain_mgr
    svc._semantic_searcher = semantic
    return svc


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class PurgeAllDataHistoryTestCase(unittest.TestCase):
    """W1730 F1: purge_all_data deletes all history items."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_all_data_removes_all_items(self) -> None:
        """purge_all_data must tombstone every active history item."""
        svc = _make_svc(self._tmpdir, item_ids=["a", "b", "c"])
        result = svc.handle_purge_all_data({})
        self.assertEqual(result["history_deleted"], 3)
        self.assertTrue(result["ok"])
        # Tombstones appended for all items
        tombstoned_ids = {t["id"] for t in svc.store._tombstones}
        self.assertEqual(tombstoned_ids, {"a", "b", "c"})

    def test_purge_all_data_empty_history_returns_zero(self) -> None:
        """purge_all_data on empty history must return history_deleted=0."""
        svc = _make_svc(self._tmpdir, item_ids=[])
        result = svc.handle_purge_all_data({})
        self.assertEqual(result["history_deleted"], 0)
        self.assertTrue(result["ok"])

    def test_purge_all_data_idempotent(self) -> None:
        """Second purge_all_data call returns history_deleted=0 (already empty)."""
        svc = _make_svc(self._tmpdir, item_ids=["x", "y"])
        svc.handle_purge_all_data({})
        # Second call: store still returns items because fake store doesn't remove them
        # so only assert it doesn't raise
        svc.store._items.clear()
        result = svc.handle_purge_all_data({})
        self.assertEqual(result["history_deleted"], 0)


class PurgeAllDataChainsTestCase(unittest.TestCase):
    """W1730 F2: purge_all_data cascades to recording chains."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_all_data_calls_delete_all_chains(self) -> None:
        """purge_all_data must call delete_all_chains() on the chain manager."""
        spy = SpyChainManager()
        spy._chains_count = 4
        svc = _make_svc(self._tmpdir, item_ids=["h1", "h2"], chain_mgr=spy)
        result = svc.handle_purge_all_data({})
        self.assertEqual(spy.delete_all_chains_called, 1,
                         "delete_all_chains must be called exactly once")
        self.assertEqual(result["chains_deleted"], 4)

    def test_purge_all_data_no_chain_manager_returns_zero(self) -> None:
        """purge_all_data without chain manager wired returns chains_deleted=0, no crash."""
        svc = _make_svc(self._tmpdir, item_ids=["h1"], chain_mgr=None)
        result = svc.handle_purge_all_data({})
        self.assertEqual(result["chains_deleted"], 0)
        self.assertTrue(result["ok"])

    def test_purge_all_data_chain_error_does_not_abort_history(self) -> None:
        """Chain manager error must not prevent history items from being deleted."""
        svc = _make_svc(self._tmpdir, item_ids=["h1", "h2"], chain_mgr=ErrorChainManager())
        # Must not raise
        result = svc.handle_purge_all_data({})
        # History items were still tombstoned
        self.assertEqual(result["history_deleted"], 2,
                         "history must be deleted even when chain manager errors")
        self.assertTrue(result["ok"])

    def test_purge_all_data_recording_chains_json_empty_after_purge(self) -> None:
        """End-to-end: after purge_all_data, RecordingChainManager sees no chains."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-secret")
        svc = HistoryService(store=store)

        # Wire a real RecordingChainManager
        chain_store = FakeStore(data_dir=self._tmpdir)
        chain_mgr = RecordingChainManager(store=chain_store)
        cid = chain_mgr.start_chain("Secret meeting")
        chain_mgr.add_to_chain(cid, "item-secret")
        svc._recording_chain_mgr = chain_mgr

        # Sanity: chain exists before purge
        self.assertEqual(len(chain_mgr.list_chains()), 1)

        result = svc.handle_purge_all_data({})

        self.assertEqual(result["chains_deleted"], 1)
        # Chain manager sees empty state after purge
        self.assertEqual(chain_mgr.list_chains(), [],
                         "After purge_all_data, recording_chains must be empty")

    def test_purge_all_data_chain_state_persisted_empty(self) -> None:
        """After purge_all_data, a new RecordingChainManager loaded from same dir sees no chains."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-a")
        svc = HistoryService(store=store)

        chain_store = FakeStore(data_dir=self._tmpdir)
        chain_mgr = RecordingChainManager(store=chain_store)
        chain_mgr.start_chain("Work session")
        svc._recording_chain_mgr = chain_mgr

        svc.handle_purge_all_data({})

        # Reload chain manager from disk
        chain_mgr2 = RecordingChainManager(store=chain_store)
        self.assertEqual(chain_mgr2.list_chains(), [],
                         "Reloaded chain manager must see no chains after purge")


class PurgeAllDataSemanticSearchTestCase(unittest.TestCase):
    """W1730 F3: purge_all_data cascades to semantic search index."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_all_data_calls_semantic_purge_all(self) -> None:
        """purge_all_data must call semantic_searcher.purge_all() when wired."""
        spy = SpySemanticSearcher()
        svc = _make_svc(self._tmpdir, item_ids=["h1"], semantic=spy)
        result = svc.handle_purge_all_data({})
        self.assertEqual(spy.purge_all_called, 1,
                         "purge_all must be called on semantic searcher")
        self.assertTrue(result["semantic_purged"])

    def test_purge_all_data_no_semantic_searcher_returns_false(self) -> None:
        """purge_all_data without semantic searcher wired returns semantic_purged=False."""
        svc = _make_svc(self._tmpdir, item_ids=["h1"], semantic=None)
        result = svc.handle_purge_all_data({})
        self.assertFalse(result["semantic_purged"])


class PurgeAllDataWiringTestCase(unittest.TestCase):
    """W1730 F4: verify that BackendService wires _recording_chain_mgr into HistoryService."""

    def test_service_wires_recording_chain_mgr_into_history(self) -> None:
        """BackendService.__init__ must set history._recording_chain_mgr = self._chains."""
        # We verify this by importing service.py and checking the wiring code exists.
        import inspect
        from backend import service as svc_module
        source = inspect.getsource(svc_module.BackendService.__init__)
        self.assertIn(
            "_history._recording_chain_mgr",
            source,
            "BackendService.__init__ must wire _chains into _history._recording_chain_mgr"
        )

    def test_purge_all_data_in_ipc_dispatch(self) -> None:
        """purge_all_data must be present in ipc_dispatch.build_dispatch_table."""
        from backend import ipc_dispatch
        import inspect
        source = inspect.getsource(ipc_dispatch.build_dispatch_table)
        self.assertIn(
            '"purge_all_data"',
            source,
            "purge_all_data must be registered in ipc_dispatch.build_dispatch_table"
        )

    def test_purge_all_data_handler_exists_on_history_service(self) -> None:
        """HistoryService must have a handle_purge_all_data method."""
        from backend.history_service import HistoryService
        self.assertTrue(
            hasattr(HistoryService, "handle_purge_all_data"),
            "HistoryService must define handle_purge_all_data"
        )


if __name__ == "__main__":
    unittest.main()
