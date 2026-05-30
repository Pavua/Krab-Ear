"""Tests for W1148 F1 HIGH: SemanticSearcher.remove() and delete_history_item wiring.

Three required tests (per W1163 spec):
  1. test_semantic_search_remove_existing_id_returns_true
  2. test_semantic_search_remove_missing_id_returns_false
  3. test_delete_history_item_removes_from_semantic_index
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher
from backend.history_service import HistoryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_model(dim: int = 4):
    """Returns a minimal mock that mimics SentenceTransformer."""
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
    """Create a SemanticSearcher with fake model and pre-populated items."""
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
# Test 1: remove() returns True for existing id
# ---------------------------------------------------------------------------

class TestSemanticSearchRemoveExistingIdReturnsTrue(unittest.TestCase):
    """SemanticSearcher.remove(history_item_id) returns True when id is in index."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(
            self.tmpdir,
            [("id-abc", "Some transcript text"), ("id-xyz", "Another transcript")],
        )

    def test_semantic_search_remove_existing_id_returns_true(self):
        result = self.searcher.remove("id-abc")
        self.assertTrue(result, "remove() should return True for an existing id")

    def test_remove_alias_delegates_to_remove_item(self):
        """remove() is a transparent alias for remove_item()."""
        with self.searcher._index_lock:
            before_count = len(self.searcher._index)
        self.searcher.remove("id-abc")
        with self.searcher._index_lock:
            after_count = len(self.searcher._index)
        self.assertEqual(after_count, before_count - 1)

    def test_remove_deletes_embedding_row(self):
        with self.searcher._index_lock:
            before_rows = self.searcher._embeddings.shape[0]
        self.searcher.remove("id-abc")
        with self.searcher._index_lock:
            after_rows = self.searcher._embeddings.shape[0]
        self.assertEqual(after_rows, before_rows - 1)

    def test_remove_item_no_longer_in_index(self):
        self.searcher.remove("id-abc")
        with self.searcher._index_lock:
            self.assertNotIn("id-abc", self.searcher._index)

    def test_other_item_still_in_index(self):
        self.searcher.remove("id-abc")
        with self.searcher._index_lock:
            self.assertIn("id-xyz", self.searcher._index)


# ---------------------------------------------------------------------------
# Test 2: remove() returns False for missing id
# ---------------------------------------------------------------------------

class TestSemanticSearchRemoveMissingIdReturnsFalse(unittest.TestCase):
    """SemanticSearcher.remove(history_item_id) returns False when id is absent."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(self.tmpdir, [("id-1", "Text")])

    def test_semantic_search_remove_missing_id_returns_false(self):
        result = self.searcher.remove("no-such-id")
        self.assertFalse(result, "remove() should return False for an absent id")

    def test_remove_missing_id_index_unchanged(self):
        with self.searcher._index_lock:
            before = list(self.searcher._index)
        self.searcher.remove("ghost-id")
        with self.searcher._index_lock:
            after = list(self.searcher._index)
        self.assertEqual(before, after, "Index must be unchanged after a no-op remove")

    def test_remove_from_empty_searcher(self):
        empty_searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test",
            enabled=True,
        )
        empty_searcher._model = _make_fake_model()
        empty_searcher._model_loaded = True
        result = empty_searcher.remove("any-id")
        self.assertFalse(result, "remove() on empty index must return False")


# ---------------------------------------------------------------------------
# Test 3: delete_history_item wires into semantic index removal
# ---------------------------------------------------------------------------

class TestDeleteHistoryItemRemovesFromSemanticIndex(unittest.TestCase):
    """HistoryService.handle_delete_history_item() calls semantic_searcher.remove()."""

    def _make_store_with_item(self, item_id: str):
        """Returns a MagicMock store that reports item_id as deletable."""
        store = MagicMock()
        store.delete_history_item.return_value = True
        return store

    def test_delete_history_item_removes_from_semantic_index(self):
        """When delete_history_item succeeds, semantic_searcher.remove is called with same id."""
        item_id = "item-to-delete"
        store = self._make_store_with_item(item_id)
        mock_searcher = MagicMock()
        mock_searcher.remove_item.return_value = True

        svc = HistoryService(store=store, semantic_searcher=mock_searcher)
        result = svc.handle_delete_history_item({"id": item_id})

        self.assertTrue(result.get("deleted"))
        # W1172: history_service calls remove_item() not bare remove()
        mock_searcher.remove_item.assert_called_once_with(item_id)

    def test_delete_history_item_no_searcher_still_works(self):
        """When no semantic_searcher provided, delete succeeds without error."""
        item_id = "item-x"
        store = MagicMock()
        store.delete_history_item.return_value = True

        svc = HistoryService(store=store, semantic_searcher=None)
        result = svc.handle_delete_history_item({"id": item_id})
        self.assertTrue(result.get("deleted"))

    def test_delete_history_item_searcher_exception_does_not_propagate(self):
        """If remove() raises, the overall delete still returns success (graceful degradation)."""
        item_id = "item-y"
        store = MagicMock()
        store.delete_history_item.return_value = True
        mock_searcher = MagicMock()
        mock_searcher.remove.side_effect = RuntimeError("index corruption")

        svc = HistoryService(store=store, semantic_searcher=mock_searcher)
        # Should NOT raise; exception is swallowed with a warning log
        result = svc.handle_delete_history_item({"id": item_id})
        self.assertTrue(result.get("deleted"))

    def test_delete_history_item_store_failure_skips_searcher(self):
        """If store delete fails (item not found), semantic_searcher.remove is NOT called."""
        item_id = "item-missing"
        store = MagicMock()
        store.delete_history_item.return_value = False
        mock_searcher = MagicMock()

        svc = HistoryService(store=store, semantic_searcher=mock_searcher)
        with self.assertRaises(ValueError):
            svc.handle_delete_history_item({"id": item_id})

        mock_searcher.remove.assert_not_called()

    def test_delete_history_item_integration_with_real_searcher(self):
        """Integration: real SemanticSearcher index is pruned after delete_history_item."""
        tmpdir = tempfile.mkdtemp()
        searcher = _make_searcher(
            tmpdir,
            [("item-to-delete", "some text"), ("keep-this", "other text")],
        )

        store = MagicMock()
        store.delete_history_item.return_value = True

        svc = HistoryService(store=store, semantic_searcher=searcher)
        svc.handle_delete_history_item({"id": "item-to-delete"})

        with searcher._index_lock:
            self.assertNotIn("item-to-delete", searcher._index)
            self.assertIn("keep-this", searcher._index)


if __name__ == "__main__":
    unittest.main()
