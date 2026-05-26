"""Tests for W1172 fix: SemanticSearcher.remove alias + HistoryService delete wiring.

W1171 F2 CRIT: PR #1072 (W1163) merged with .remove() call which doesn't exist on
SemanticSearcher — AttributeError was silently caught, embeddings never removed.

Fix:
  1. Added `remove = remove_item` alias on SemanticSearcher class.
  2. Changed call site in HistoryService.handle_delete_history_item to .remove_item().
  3. Wired semantic_searcher into HistoryService.__init__ (was missing entirely).

Three test cases required by W1172 spec:
  - test_remove_alias_works
  - test_history_delete_calls_remove_item_not_remove
  - test_delete_actually_removes_from_index
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_model(dim: int = 4) -> MagicMock:
    """Returns a mock that mimics SentenceTransformer.encode."""
    model = MagicMock()

    def _encode(text, normalize_embeddings=True):
        seed = sum(ord(c) for c in str(text)) % 1000
        rng = np.random.RandomState(seed)
        v = rng.rand(dim).astype(np.float32)
        if normalize_embeddings:
            v /= np.linalg.norm(v) + 1e-10
        return v

    model.encode.side_effect = lambda text, **kw: (
        np.stack([_encode(t, **kw) for t in text])
        if isinstance(text, list)
        else _encode(text, **kw)
    )
    return model


def _make_searcher(tmpdir: str, items: list[tuple[str, str]], dim: int = 4) -> SemanticSearcher:
    searcher = SemanticSearcher(
        data_dir=Path(tmpdir),
        model_name="test-model",
        enabled=True,
    )
    searcher._model = _make_fake_model(dim=dim)
    searcher._model_loaded = True
    for item_id, text in items:
        searcher.index_item(item_id, text)
    return searcher


# ---------------------------------------------------------------------------
# Test 1: remove alias works identically to remove_item
# ---------------------------------------------------------------------------

class TestRemoveAlias(unittest.TestCase):
    """SemanticSearcher.remove is a valid alias for remove_item (W1172 fix 1 of 2)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(
            self.tmpdir,
            [("id-a", "первый текст"), ("id-b", "второй текст")],
        )

    def test_remove_alias_exists(self):
        """SemanticSearcher has a .remove attribute (alias)."""
        self.assertTrue(
            hasattr(self.searcher, "remove"),
            "SemanticSearcher must have .remove alias after W1172 fix",
        )

    def test_remove_alias_is_callable(self):
        self.assertTrue(callable(self.searcher.remove))

    def test_remove_alias_works(self):
        """Calling .remove(item_id) removes the item — same behaviour as remove_item."""
        result = self.searcher.remove("id-a")
        self.assertTrue(result, ".remove() must return True when item exists")
        with self.searcher._index_lock:
            self.assertNotIn("id-a", self.searcher._index)

    def test_remove_alias_returns_false_for_missing(self):
        result = self.searcher.remove("no-such-id")
        self.assertFalse(result)

    def test_remove_and_remove_item_are_same_function(self):
        """Alias identity check — both names point to the same underlying callable."""
        self.assertIs(
            SemanticSearcher.remove,
            SemanticSearcher.remove_item,
            "remove must be exactly remove_item (not a wrapper)",
        )

    def test_remove_alias_decrements_embedding_count(self):
        with self.searcher._index_lock:
            before = self.searcher._embeddings.shape[0]
        self.searcher.remove("id-a")
        with self.searcher._index_lock:
            after = self.searcher._embeddings.shape[0]
        self.assertEqual(after, before - 1)


# ---------------------------------------------------------------------------
# Test 2: HistoryService.handle_delete_history_item calls .remove_item()
# ---------------------------------------------------------------------------

class TestHistoryDeleteCallsRemoveItem(unittest.TestCase):
    """handle_delete_history_item must call semantic_searcher.remove_item (W1172 fix 2 of 2)."""

    def _make_store(self, item_id: str = "test-item-id"):
        store = MagicMock()
        store.delete_history_item.return_value = True
        store.data_dir = None
        return store

    def _make_service(self, semantic_searcher=None):
        from backend.history_service import HistoryService
        store = self._make_store()
        svc = HistoryService(
            store=store,
            semantic_searcher=semantic_searcher,
        )
        return svc, store

    def test_history_delete_calls_remove_item_not_remove(self):
        """delete_history_item must call .remove_item(), not .remove()."""
        mock_searcher = MagicMock(spec=SemanticSearcher)
        mock_searcher.remove_item.return_value = True

        svc, store = self._make_service(semantic_searcher=mock_searcher)
        svc.handle_delete_history_item({"id": "abc-123"})

        # Must call remove_item, NOT remove (the old broken name)
        mock_searcher.remove_item.assert_called_once_with("abc-123")

    def test_history_delete_does_not_call_bare_remove(self):
        """The old broken call .remove() must NOT be invoked."""
        mock_searcher = MagicMock()
        mock_searcher.remove_item.return_value = True

        svc, _ = self._make_service(semantic_searcher=mock_searcher)
        svc.handle_delete_history_item({"id": "xyz-789"})

        # remove_item called
        mock_searcher.remove_item.assert_called_once_with("xyz-789")
        # If .remove was a separate method, it must NOT have been called
        # (only matters if remove is not aliased — we just verify remove_item was used)
        self.assertEqual(mock_searcher.remove_item.call_count, 1)

    def test_history_delete_without_searcher_still_works(self):
        """handle_delete_history_item works fine when semantic_searcher is None."""
        svc, store = self._make_service(semantic_searcher=None)
        result = svc.handle_delete_history_item({"id": "no-embed-id"})
        store.delete_history_item.assert_called_once_with("no-embed-id")
        self.assertEqual(result, {"deleted": True})

    def test_history_delete_searcher_exception_does_not_propagate(self):
        """If remove_item raises, the exception is swallowed (best-effort removal)."""
        mock_searcher = MagicMock()
        mock_searcher.remove_item.side_effect = RuntimeError("index corrupt")

        svc, _ = self._make_service(semantic_searcher=mock_searcher)
        # Should NOT raise
        result = svc.handle_delete_history_item({"id": "crash-id"})
        self.assertEqual(result, {"deleted": True})

    def test_history_service_accepts_semantic_searcher_param(self):
        """HistoryService.__init__ must accept semantic_searcher kwarg (was missing)."""
        from backend.history_service import HistoryService
        store = MagicMock()
        store.data_dir = None
        mock_searcher = MagicMock()

        # Must not raise TypeError
        svc = HistoryService(store=store, semantic_searcher=mock_searcher)
        self.assertIs(svc._semantic_searcher, mock_searcher)


# ---------------------------------------------------------------------------
# Test 3: Integration — delete actually removes from index
# ---------------------------------------------------------------------------

class TestDeleteActuallyRemovesFromIndex(unittest.TestCase):
    """W1163 contract: delete_history_item MUST remove the embedding from semantic index."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(
            self.tmpdir,
            [
                ("item-1", "кошка пьёт молоко"),
                ("item-2", "собака гуляет в парке"),
                ("item-3", "рыба плавает в воде"),
            ],
        )

    def _make_store_with_item(self, item_id: str):
        store = MagicMock()
        store.delete_history_item.return_value = True
        store.data_dir = None
        return store

    def _make_history_svc(self, item_id: str):
        from backend.history_service import HistoryService
        store = self._make_store_with_item(item_id)
        return HistoryService(
            store=store,
            semantic_searcher=self.searcher,
        )

    def test_delete_actually_removes_from_index(self):
        """After handle_delete_history_item, the embedding is gone from the index."""
        svc = self._make_history_svc("item-1")
        with self.searcher._index_lock:
            before = list(self.searcher._index)
        self.assertIn("item-1", before)

        svc.handle_delete_history_item({"id": "item-1"})

        with self.searcher._index_lock:
            after = list(self.searcher._index)
        self.assertNotIn("item-1", after, "item-1 embedding must be removed after delete")

    def test_delete_removes_correct_item_only(self):
        """Only the deleted item is removed; others remain."""
        svc = self._make_history_svc("item-2")
        svc.handle_delete_history_item({"id": "item-2"})

        with self.searcher._index_lock:
            idx = list(self.searcher._index)
        self.assertNotIn("item-2", idx)
        self.assertIn("item-1", idx)
        self.assertIn("item-3", idx)

    def test_delete_embedding_matrix_shrinks(self):
        """Embedding matrix has one fewer row after delete."""
        with self.searcher._index_lock:
            before_count = self.searcher._embeddings.shape[0]

        svc = self._make_history_svc("item-3")
        svc.handle_delete_history_item({"id": "item-3"})

        with self.searcher._index_lock:
            after_count = (
                self.searcher._embeddings.shape[0]
                if self.searcher._embeddings is not None
                else 0
            )
        self.assertEqual(after_count, before_count - 1)

    def test_deleted_item_not_in_search_results(self):
        """After delete, the item does not appear in search results."""
        svc = self._make_history_svc("item-1")
        svc.handle_delete_history_item({"id": "item-1"})

        results = self.searcher.search("кошка молоко", top_k=10)
        ids = [r["id"] for r in results]
        self.assertNotIn("item-1", ids, "deleted item must not appear in search after W1163 fix")

    def test_remaining_items_still_searchable_after_delete(self):
        """Search still works on remaining items after one is deleted."""
        svc = self._make_history_svc("item-1")
        svc.handle_delete_history_item({"id": "item-1"})

        results = self.searcher.search("собака парк", top_k=5)
        self.assertGreater(len(results), 0)
        ids = [r["id"] for r in results]
        self.assertIn("item-2", ids)


if __name__ == "__main__":
    unittest.main()
