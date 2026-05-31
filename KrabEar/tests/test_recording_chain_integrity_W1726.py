"""Regression tests for recording_chain data-integrity bugs fixed in W1726.

Covers four bugs:
  BUG1 (MED, duplicate) — replace_items_in_chain: when new_id already present
        before any old_id, it was inserted again → duplicate entry.
  BUG2 (MED, phantom ids) — handle_cleanup_old_history: missing chain cascade
        left phantom item_ids in recording_chains.json.
  BUG3 (MED, race) — list_chains: post-lock reads of shared dict objects
        could produce torn item_count / ended_at under concurrent mutation.
        (Tested indirectly via correctness; threading proof included.)
  BUG4 (LOW, input validation) — handle_list_chains: int() on non-numeric
        limit raised uncaught ValueError → 500-level IPC error.
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
    def __init__(self, item_id: str, text: str = "hello",
                 ts: str = "2020-01-01T00:00:00+00:00") -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.duration_sec = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "duration_sec": self.duration_sec}


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()

    def add_item(self, item_id: str, text: str = "hello",
                 ts: str = "2020-01-01T00:00:00+00:00") -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text, ts)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)

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
# BUG1: replace_items_in_chain — no duplicate when new_id already present
# ---------------------------------------------------------------------------

class TestReplaceItemsInChainNoDuplicate(unittest.TestCase):
    """BUG1 (W1726): new_id already present before first old_id must NOT
    be inserted again at the old_id position."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def _make_chain(self, item_ids: list[str]) -> str:
        chain_id = self._mgr.start_chain("Test chain")
        for iid in item_ids:
            self._mgr.add_to_chain(chain_id, iid)
        return chain_id

    def test_no_duplicate_when_new_id_already_at_start(self) -> None:
        """['merged','a','orig1','b'] replacing orig1→merged must yield
        ['merged','a','b'] — not ['merged','a','merged','b']."""
        chain_id = self._make_chain(["merged", "a", "orig1", "b"])

        changed = self._mgr.replace_items_in_chain(chain_id, ["orig1"], "merged")

        self.assertTrue(changed)
        chain = self._mgr.get_chain(chain_id)
        self.assertEqual(chain["item_ids"].count("merged"), 1,
                         f"Expected exactly 1 'merged', got: {chain['item_ids']}")
        self.assertNotIn("orig1", chain["item_ids"])
        self.assertEqual(chain["item_ids"], ["merged", "a", "b"])

    def test_no_duplicate_when_new_id_in_middle_before_old_ids(self) -> None:
        """['a','merged','orig1','orig2','b'] replacing orig1,orig2→merged
        must yield ['a','merged','b']."""
        chain_id = self._make_chain(["a", "merged", "orig1", "orig2", "b"])

        changed = self._mgr.replace_items_in_chain(chain_id, ["orig1", "orig2"], "merged")

        self.assertTrue(changed)
        chain = self._mgr.get_chain(chain_id)
        self.assertEqual(chain["item_ids"].count("merged"), 1,
                         f"Expected exactly 1 'merged', got: {chain['item_ids']}")
        self.assertEqual(chain["item_ids"], ["a", "merged", "b"])

    def test_replace_when_new_id_not_yet_present_still_inserts(self) -> None:
        """Normal path: new_id not yet in chain → inserts at first old_id position."""
        chain_id = self._make_chain(["a", "orig1", "orig2", "b"])

        changed = self._mgr.replace_items_in_chain(chain_id, ["orig1", "orig2"], "merged-new")

        self.assertTrue(changed)
        chain = self._mgr.get_chain(chain_id)
        self.assertEqual(chain["item_ids"], ["a", "merged-new", "b"])
        self.assertEqual(chain["item_ids"].count("merged-new"), 1)

    def test_replace_all_old_ids_with_same_new_id_exactly_once(self) -> None:
        """Multiple old_ids → new_id appears exactly once at first match position."""
        chain_id = self._make_chain(["x", "orig1", "orig2", "orig3", "y"])

        self._mgr.replace_items_in_chain(chain_id, ["orig1", "orig2", "orig3"], "merged")

        chain = self._mgr.get_chain(chain_id)
        self.assertEqual(chain["item_ids"], ["x", "merged", "y"])
        self.assertEqual(chain["item_ids"].count("merged"), 1)


# ---------------------------------------------------------------------------
# BUG2: handle_cleanup_old_history — chain cascade for bulk-deleted items
# ---------------------------------------------------------------------------

class TestCleanupOldHistoryChainCascade(unittest.TestCase):
    """BUG2 (W1726): handle_cleanup_old_history must call
    remove_item_from_all_chains for each age-deleted item when
    _recording_chain_mgr is wired."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)

    def _make_history_service(self):
        from backend.history_service import HistoryService
        return HistoryService(store=self._store)

    def test_cleanup_old_history_removes_items_from_chains(self) -> None:
        """Items deleted by cleanup_old_history must be removed from chains
        (no phantom item_ids left behind)."""
        # Items older than 1 day (cutoff=1 day)
        old_ts = "2000-01-01T00:00:00+00:00"
        recent_ts = "2099-12-31T23:59:59+00:00"
        self._store.add_item("old-item", ts=old_ts)
        self._store.add_item("recent-item", ts=recent_ts)

        chain_mgr = RecordingChainManager(store=self._store)
        cid = chain_mgr.start_chain("Mixed age chain")
        chain_mgr.add_to_chain(cid, "old-item")
        chain_mgr.add_to_chain(cid, "recent-item")

        svc = self._make_history_service()
        svc._recording_chain_mgr = chain_mgr  # late-injection

        result = svc.handle_cleanup_old_history({"older_than_days": 1})

        self.assertEqual(result["deleted_count"], 1)
        # Phantom check: old-item must be gone from chain
        chain = chain_mgr.get_chain(cid)
        self.assertNotIn("old-item", chain["item_ids"],
                         "old-item must be removed from chain after bulk delete")
        self.assertIn("recent-item", chain["item_ids"],
                      "recent-item must remain in chain")

    def test_cleanup_old_history_multiple_items_all_cascaded(self) -> None:
        """All age-deleted items are cascaded out of chains."""
        old_ts = "2000-01-01T00:00:00+00:00"
        for i in range(3):
            self._store.add_item(f"old-{i}", ts=old_ts)

        chain_mgr = RecordingChainManager(store=self._store)
        cid = chain_mgr.start_chain("Old meeting")
        for i in range(3):
            chain_mgr.add_to_chain(cid, f"old-{i}")

        svc = self._make_history_service()
        svc._recording_chain_mgr = chain_mgr

        result = svc.handle_cleanup_old_history({"older_than_days": 1})

        self.assertEqual(result["deleted_count"], 3)
        chain = chain_mgr.get_chain(cid)
        for i in range(3):
            self.assertNotIn(f"old-{i}", chain["item_ids"])
        self.assertEqual(chain["item_ids"], [])

    def test_cleanup_old_history_no_chain_mgr_still_works(self) -> None:
        """Without _recording_chain_mgr, cleanup succeeds without AttributeError."""
        old_ts = "2000-01-01T00:00:00+00:00"
        self._store.add_item("old-orphan", ts=old_ts)

        svc = self._make_history_service()
        # _recording_chain_mgr is None by default

        result = svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 1)

    def test_cleanup_old_history_no_deletes_no_chain_calls(self) -> None:
        """When nothing is old enough to delete, chain manager is not touched."""
        future_ts = "2099-12-31T23:59:59+00:00"
        self._store.add_item("future-item", ts=future_ts)

        chain_mgr = RecordingChainManager(store=self._store)
        cid = chain_mgr.start_chain("Future chain")
        chain_mgr.add_to_chain(cid, "future-item")

        svc = self._make_history_service()
        svc._recording_chain_mgr = chain_mgr

        result = svc.handle_cleanup_old_history({"older_than_days": 1})

        self.assertEqual(result["deleted_count"], 0)
        # Item still intact in chain
        chain = chain_mgr.get_chain(cid)
        self.assertIn("future-item", chain["item_ids"])


# ---------------------------------------------------------------------------
# BUG3: list_chains — lock-safe snapshot (concurrent mutation correctness)
# ---------------------------------------------------------------------------

class TestListChainsLockSafe(unittest.TestCase):
    """BUG3 (W1726): list_chains must build result snapshots inside the lock
    so concurrent add_to_chain / end_chain cannot produce torn item_count."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def test_list_chains_item_count_matches_actual(self) -> None:
        """After N adds, list_chains.item_count equals get_chain.item_count."""
        cid = self._mgr.start_chain("Count test")
        for i in range(5):
            self._mgr.add_to_chain(cid, f"item-{i}")

        chains = self._mgr.list_chains()
        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0]["item_count"], 5)

    def test_list_chains_concurrent_add_no_crash(self) -> None:
        """Concurrent add_to_chain while list_chains runs must not crash."""
        cid = self._mgr.start_chain("Concurrent chain")
        for i in range(10):
            self._mgr.add_to_chain(cid, f"seed-{i}")

        errors: list[Exception] = []

        def do_list():
            try:
                for _ in range(20):
                    result = self._mgr.list_chains()
                    # Each returned summary must have non-negative item_count
                    for c in result:
                        assert c["item_count"] >= 0, f"negative item_count: {c}"
            except Exception as exc:
                errors.append(exc)

        def do_add(n: int):
            try:
                for i in range(5):
                    self._mgr.add_to_chain(cid, f"concurrent-{n}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=do_list) for _ in range(3)]
            + [threading.Thread(target=do_add, args=(n,)) for n in range(4)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")

    def test_list_chains_ended_at_consistent(self) -> None:
        """list_chains returns ended_at correctly after end_chain."""
        cid = self._mgr.start_chain("Ended chain")
        self._mgr.end_chain(cid)

        chains = self._mgr.list_chains()
        self.assertEqual(len(chains), 1)
        self.assertIsNotNone(chains[0]["ended_at"],
                             "ended_at must be non-None after end_chain")


# ---------------------------------------------------------------------------
# BUG4: handle_list_chains — graceful handling of non-numeric limit
# ---------------------------------------------------------------------------

class TestHandleListChainsLimitValidation(unittest.TestCase):
    """BUG4 (W1726): handle_list_chains must not raise ValueError on
    non-numeric limit input."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)
        # Populate a few chains for non-empty results
        for i in range(3):
            self._mgr.start_chain(f"Chain {i}")

    def test_limit_all_string_no_crash(self) -> None:
        """handle_list_chains with limit='all' must not raise ValueError."""
        try:
            result = self._mgr.handle_list_chains({"limit": "all"})
        except ValueError as exc:
            self.fail(f"handle_list_chains raised ValueError for limit='all': {exc}")
        self.assertIn("chains", result)
        self.assertIsInstance(result["chains"], list)

    def test_limit_none_string_no_crash(self) -> None:
        """handle_list_chains with limit='none' uses default and returns chains."""
        result = self._mgr.handle_list_chains({"limit": "none"})
        self.assertIn("chains", result)

    def test_limit_float_string_no_crash(self) -> None:
        """handle_list_chains with limit='2.5' (float string) falls back to default."""
        result = self._mgr.handle_list_chains({"limit": "2.5"})
        self.assertIn("chains", result)

    def test_limit_numeric_string_still_works(self) -> None:
        """handle_list_chains with limit='2' (numeric string) works correctly."""
        result = self._mgr.handle_list_chains({"limit": "2"})
        self.assertIn("chains", result)
        self.assertLessEqual(len(result["chains"]), 2)

    def test_limit_integer_still_works(self) -> None:
        """handle_list_chains with integer limit continues to work as before."""
        result = self._mgr.handle_list_chains({"limit": 2})
        self.assertIn("chains", result)
        self.assertEqual(len(result["chains"]), 2)

    def test_limit_missing_uses_default(self) -> None:
        """handle_list_chains without limit param returns up to 20 chains."""
        result = self._mgr.handle_list_chains({})
        self.assertIn("chains", result)
        self.assertLessEqual(len(result["chains"]), 20)

    def test_limit_none_value_uses_default(self) -> None:
        """handle_list_chains with limit=None falls back to default gracefully."""
        result = self._mgr.handle_list_chains({"limit": None})
        self.assertIn("chains", result)


if __name__ == "__main__":
    unittest.main()
