"""Tests for SemanticSearcher.remove_item() — Wave 156.

5 test cases:
  1. test_remove_existing_returns_true
  2. test_remove_nonexistent_returns_false
  3. test_remove_updates_index_correctly
  4. test_remove_subsequent_search_works
  5. test_concurrent_remove_thread_safe
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_model(dim: int = 4):
    """Returns a mock that mimics SentenceTransformer.encode."""
    from unittest.mock import MagicMock

    model = MagicMock()

    def _encode(text, normalize_embeddings=True):
        seed = sum(ord(c) for c in str(text)) % 1000
        rng = np.random.RandomState(seed)
        v = rng.rand(dim).astype(np.float32)
        if normalize_embeddings:
            v /= np.linalg.norm(v) + 1e-10
        return v

    def _encode_batch(texts, normalize_embeddings=True):
        return np.stack([_encode(t, normalize_embeddings) for t in texts])

    model.encode.side_effect = lambda text, **kw: (
        _encode_batch(text, **kw) if isinstance(text, list) else _encode(text, **kw)
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
# Tests
# ---------------------------------------------------------------------------

class TestRemoveExistingReturnsTrue(unittest.TestCase):
    """remove_item returns True when the item exists in the index."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(
            self.tmpdir,
            [("id1", "Привет мир"), ("id2", "Как дела")],
        )

    def test_remove_existing_returns_true(self):
        result = self.searcher.remove_item("id1")
        self.assertTrue(result)

    def test_removed_item_no_longer_in_index(self):
        self.searcher.remove_item("id1")
        with self.searcher._index_lock:
            self.assertNotIn("id1", self.searcher._index)

    def test_embeddings_row_count_decremented(self):
        with self.searcher._index_lock:
            before = self.searcher._embeddings.shape[0]
        self.searcher.remove_item("id1")
        with self.searcher._index_lock:
            after = self.searcher._embeddings.shape[0]
        self.assertEqual(after, before - 1)

    def test_remaining_item_still_in_index(self):
        self.searcher.remove_item("id1")
        with self.searcher._index_lock:
            self.assertIn("id2", self.searcher._index)


class TestRemoveNonexistentReturnsFalse(unittest.TestCase):
    """remove_item returns False when item_id is not in the index."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(self.tmpdir, [("id1", "Текст")])

    def test_remove_nonexistent_returns_false(self):
        result = self.searcher.remove_item("no-such-id")
        self.assertFalse(result)

    def test_index_unchanged_after_false_remove(self):
        with self.searcher._index_lock:
            before = list(self.searcher._index)
        self.searcher.remove_item("no-such-id")
        with self.searcher._index_lock:
            after = list(self.searcher._index)
        self.assertEqual(before, after)

    def test_remove_from_empty_index(self):
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        searcher._model = _make_fake_model()
        searcher._model_loaded = True
        result = searcher.remove_item("ghost")
        self.assertFalse(result)

    def test_remove_last_item_sets_embeddings_none(self):
        """After removing the only item, _embeddings becomes None."""
        searcher = _make_searcher(self.tmpdir + "_single", [("only", "один единственный")])
        searcher.remove_item("only")
        with searcher._index_lock:
            self.assertIsNone(searcher._embeddings)
            self.assertEqual(len(searcher._index), 0)


class TestRemoveUpdatesIndexCorrectly(unittest.TestCase):
    """Row positions remain consistent after remove (list-based index)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Three items at rows 0, 1, 2
        self.searcher = _make_searcher(
            self.tmpdir,
            [
                ("alpha", "первый"),
                ("beta", "второй"),
                ("gamma", "третий"),
            ],
        )

    def test_remove_middle_item_index_compact(self):
        """After removing 'beta' (row 1), 'gamma' moves to row 1."""
        self.searcher.remove_item("beta")
        with self.searcher._index_lock:
            idx = self.searcher._index
            embeddings = self.searcher._embeddings
        self.assertEqual(len(idx), 2)
        self.assertEqual(embeddings.shape[0], 2)
        self.assertIn("alpha", idx)
        self.assertIn("gamma", idx)
        self.assertNotIn("beta", idx)

    def test_remove_first_item(self):
        self.searcher.remove_item("alpha")
        with self.searcher._index_lock:
            idx = self.searcher._index
        self.assertEqual(idx[0], "beta")
        self.assertEqual(idx[1], "gamma")

    def test_remove_last_item_in_list(self):
        self.searcher.remove_item("gamma")
        with self.searcher._index_lock:
            idx = self.searcher._index
        self.assertEqual(len(idx), 2)
        self.assertNotIn("gamma", idx)

    def test_double_remove_second_call_returns_false(self):
        self.searcher.remove_item("beta")
        result = self.searcher.remove_item("beta")
        self.assertFalse(result)


class TestRemoveSubsequentSearchWorks(unittest.TestCase):
    """Search continues to work correctly after remove_item calls."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(
            self.tmpdir,
            [
                ("cat", "кошка животное"),
                ("dog", "собака животное"),
                ("fish", "рыба вода"),
            ],
            dim=8,
        )

    def test_removed_item_not_in_search_results(self):
        self.searcher.remove_item("cat")
        results = self.searcher.search("кошка животное", top_k=10)
        ids = [r["id"] for r in results]
        self.assertNotIn("cat", ids)

    def test_remaining_items_still_searchable(self):
        self.searcher.remove_item("fish")
        results = self.searcher.search("животное", top_k=5)
        self.assertGreater(len(results), 0)
        ids = [r["id"] for r in results]
        self.assertNotIn("fish", ids)

    def test_search_on_empty_index_after_remove_all(self):
        for item_id in ["cat", "dog", "fish"]:
            self.searcher.remove_item(item_id)
        results = self.searcher.search("животное", top_k=5)
        self.assertEqual(results, [])

    def test_index_and_search_after_remove_and_reindex(self):
        """After remove + re-index, item is searchable again."""
        self.searcher.remove_item("cat")
        self.searcher.index_item("cat", "кошка животное")
        results = self.searcher.search("кошка", top_k=5)
        ids = [r["id"] for r in results]
        self.assertIn("cat", ids)


class TestConcurrentRemoveThreadSafe(unittest.TestCase):
    """remove_item is thread-safe under concurrent access."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        n = 20
        items = [(f"item{i}", f"текст номер {i}") for i in range(n)]
        self.searcher = _make_searcher(self.tmpdir, items, dim=4)
        self.n = n

    def test_concurrent_remove_thread_safe(self):
        """Concurrently remove all items; no exceptions and index stays consistent."""
        errors: list[Exception] = []
        threads = []

        def _remove(item_id: str) -> None:
            try:
                self.searcher.remove_item(item_id)
            except Exception as exc:
                errors.append(exc)

        for i in range(self.n):
            t = threading.Thread(target=_remove, args=(f"item{i}",))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        with self.searcher._index_lock:
            remaining = len(self.searcher._index)
            emb = self.searcher._embeddings
        # All removed: index empty, embeddings None
        self.assertEqual(remaining, 0)
        self.assertIsNone(emb)

    def test_concurrent_remove_and_index(self):
        """Mix of remove and index_item calls completes without corruption."""
        errors: list[Exception] = []

        def _remove_and_add(i: int) -> None:
            try:
                self.searcher.remove_item(f"item{i}")
                self.searcher.index_item(f"new{i}", f"новый текст {i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_remove_and_add, args=(i,)) for i in range(self.n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        with self.searcher._index_lock:
            # Index should be consistent: each id appears at most once
            idx = self.searcher._index
        self.assertEqual(len(idx), len(set(idx)), "Duplicate ids in index after concurrent ops")


if __name__ == "__main__":
    unittest.main()
