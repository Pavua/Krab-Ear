"""Tests for W1148 F1 HIGH: semantic_search cleanup on delete_history_item.

Verifies that:
1. delete_history_item invokes semantic_searcher.remove_item(item_id)
2. Calling delete twice (idempotent second call via store) is safe
3. Errors from remove_item are soft-failed (no exception propagation)
"""
from __future__ import annotations

import sys
import os
import unittest
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeStore:
    """Minimal StateStore stub."""
    def __init__(self, item_ids: list[str] | None = None):
        import tempfile
        self._items = set(item_ids or [])
        self.deleted: list[str] = []
        # wave-1762: _erase_transcript_md needs a real data_dir to construct paths.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = self._tmpdir.name

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._items.discard(item_id)
            self.deleted.append(item_id)
            return True
        return False

    def _lock(self):
        """wave-27: handle_delete_history_item acquires store._lock() before checking existence."""
        import contextlib
        return contextlib.nullcontext()

    def _load_active_items_unlocked(self):
        """wave-1762: called inside _lock() to resolve item timestamps for cascade."""
        from types import SimpleNamespace
        return [SimpleNamespace(id=i, ts="2026-01-01T00:00:00Z") for i in self._items]

    # Minimal attrs used by HistoryService.__init__
    data_dir = None


class _FakeSemanticSearcher:
    """Minimal SemanticSearcher stub that records calls."""
    def __init__(self):
        self.removed: list[str] = []
        self.raise_on_remove: Exception | None = None

    def remove_item(self, item_id: str) -> bool:
        if self.raise_on_remove is not None:
            raise self.raise_on_remove
        self.removed.append(item_id)
        return item_id in ("existing-id",)


class TestSemanticSearchDeleteWiring(unittest.TestCase):
    """delete_history_item should invoke semantic_searcher.remove_item."""

    def _make_service(self, item_ids=("abc123",)):
        from backend.history_service import HistoryService
        store = _FakeStore(list(item_ids))
        svc = HistoryService(store=store)
        searcher = _FakeSemanticSearcher()
        svc._semantic_searcher = searcher
        return svc, store, searcher

    # ------------------------------------------------------------------
    # Test 1: delete invokes remove_item
    # ------------------------------------------------------------------
    def test_delete_invokes_remove_item(self):
        svc, store, searcher = self._make_service(["item-1"])
        result = svc.handle_delete_history_item({"id": "item-1"})
        self.assertTrue(result["deleted"])
        self.assertIn("item-1", store.deleted)
        self.assertIn("item-1", searcher.removed,
                      "remove_item должен быть вызван с правильным item_id")

    # ------------------------------------------------------------------
    # Test 2: idempotent — second delete raises ValueError (store returns False)
    #         but the first remove_item was already called once
    # ------------------------------------------------------------------
    def test_second_delete_raises_not_crashes_index(self):
        svc, store, searcher = self._make_service(["item-2"])
        # First delete succeeds
        svc.handle_delete_history_item({"id": "item-2"})
        self.assertEqual(len(searcher.removed), 1)
        # Second delete should raise ValueError (not found in store)
        with self.assertRaises(ValueError):
            svc.handle_delete_history_item({"id": "item-2"})
        # remove_item must NOT be called again (store returned False → exception before remove)
        self.assertEqual(len(searcher.removed), 1,
                         "remove_item не должен вызываться повторно при отсутствии записи")

    # ------------------------------------------------------------------
    # Test 3: soft-fail — exception from remove_item must NOT propagate
    # ------------------------------------------------------------------
    def test_remove_item_error_is_soft_failed(self):
        svc, store, searcher = self._make_service(["item-3"])
        searcher.raise_on_remove = RuntimeError("index broken")
        # Should not raise even though remove_item throws
        try:
            result = svc.handle_delete_history_item({"id": "item-3"})
        except Exception as exc:
            self.fail(
                f"handle_delete_history_item не должен пробрасывать ошибки "
                f"из remove_item, но получили: {exc}"
            )
        self.assertTrue(result["deleted"],
                        "Удаление из store должно работать даже при ошибке индекса")

    # ------------------------------------------------------------------
    # Test 4: no semantic_searcher wired — works without error
    # ------------------------------------------------------------------
    def test_no_semantic_searcher_still_deletes(self):
        from backend.history_service import HistoryService
        store = _FakeStore(["item-4"])
        svc = HistoryService(store=store)
        # _semantic_searcher is None by default
        result = svc.handle_delete_history_item({"id": "item-4"})
        self.assertTrue(result["deleted"])


if __name__ == "__main__":
    unittest.main()
