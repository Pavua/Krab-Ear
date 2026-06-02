"""wave-22 regression tests for SemanticSearcher purge-vs-index race + cap + save-fail surface.

Covers the MED + 2 LOW fixes:

  MED (purge barrier): index_item / index_all compute the embedding (slow _encode)
      BEFORE re-acquiring _index_lock. If handle_purge_all_data → purge_all() runs
      while the encode is in flight, the in-flight thread must NOT re-add the
      now-purged cleartext-derived embedding nor re-create embeddings.npy /
      embeddings_index.json. A monotonic _purge_epoch bumped under _index_lock in
      purge_all() acts as a hard barrier: epoch mismatch at the post-encode re-lock
      → abort, persist nothing.

  LOW (cap): SEMANTIC_SEARCH_MAX_ITEMS bounds the index; oldest rows are evicted
      (FIFO / most-recent-N) so it cannot grow unbounded.

  LOW (save-fail surface): a _save_locked persistence failure (previously swallowed
      with a warning while index_item still returned success) is also pushed to
      _error_bus when one is wired.

All tests use small fake vectors / monkeypatched _encode — the real e5 model is
never loaded. They are deterministic (no sleeps / no thread races) and fast.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher


def _fake_vec(dim: int = 4):
    import numpy as np
    return np.ones(dim, dtype=np.float32) / float(dim) ** 0.5


def _make_searcher(tmpdir, max_items: int = 0):
    searcher = SemanticSearcher(
        data_dir=Path(tmpdir),
        model_name="test-model",
        enabled=True,
        max_items=max_items,
    )
    # Pretend the model is loaded so _get_model() returns a truthy object without
    # touching sentence-transformers.
    searcher._model = object()
    searcher._model_loaded = True
    return searcher


class TestPurgeRaceBarrier(unittest.TestCase):
    """MED: a purge straddling _encode must abort the in-flight re-add."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(self.tmpdir)

    def test_purge_during_encode_aborts_reindex_and_no_files(self):
        """The canonical race: purge fires mid-encode → index stays empty,
        embeddings.npy / embeddings_index.json are NOT (re-)created."""
        searcher = self.searcher

        # Monkeypatch _encode to fire a purge_all() *during* the encode — exactly
        # the window the daemon index thread is vulnerable in. The embedding it
        # returns is therefore "stale" relative to the purge that just happened.
        def _encode_then_purge(model, text):
            searcher.purge_all()  # bumps _purge_epoch under _index_lock
            return _fake_vec()

        searcher._encode = _encode_then_purge

        ok = searcher.index_item("secret-id", "сверхсекретный транскрипт")

        # The in-flight add must be refused.
        self.assertFalse(ok)
        with searcher._index_lock:
            self.assertEqual(searcher._index, [])
            self.assertIsNone(searcher._embeddings)

        # And — the whole point of the fix — no persisted file may exist with a
        # cleartext-derived embedding of the just-purged transcript.
        self.assertFalse(
            searcher._embeddings_path.exists(),
            "embeddings.npy was re-created after purge (PII leak)",
        )
        self.assertFalse(
            searcher._index_path.exists(),
            "embeddings_index.json was re-created after purge (PII leak)",
        )

    def test_purge_epoch_increments_on_purge(self):
        before = self.searcher._purge_epoch
        self.searcher.purge_all()
        self.assertEqual(self.searcher._purge_epoch, before + 1)
        self.searcher.purge_all()
        self.assertEqual(self.searcher._purge_epoch, before + 2)

    def test_no_purge_during_encode_indexes_normally(self):
        """Sanity: without a concurrent purge, indexing still works and persists."""
        self.searcher._encode = lambda model, text: _fake_vec()
        ok = self.searcher.index_item("id1", "обычный текст")
        self.assertTrue(ok)
        with self.searcher._index_lock:
            self.assertIn("id1", self.searcher._index)
        self.assertTrue(self.searcher._embeddings_path.exists())
        self.assertTrue(self.searcher._index_path.exists())

    def test_purge_clears_files_already_on_disk(self):
        """A purge after a normal persisted index removes the files (baseline that
        the barrier protects — purge itself must win)."""
        searcher = self.searcher
        searcher._encode = lambda model, text: _fake_vec()
        self.assertTrue(searcher.index_item("id1", "первый"))
        self.assertTrue(searcher._embeddings_path.exists())
        self.assertTrue(searcher._index_path.exists())

        searcher.purge_all()
        self.assertFalse(searcher._embeddings_path.exists())
        self.assertFalse(searcher._index_path.exists())
        with searcher._index_lock:
            self.assertEqual(searcher._index, [])
            self.assertIsNone(searcher._embeddings)


class TestIndexAllPurgeBarrier(unittest.TestCase):
    """MED: same barrier for the batch path (index_all)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(self.tmpdir)

    def test_index_all_aborts_when_purge_straddles_batch_encode(self):
        searcher = self.searcher

        def _encode_batch_then_purge(model, texts):
            import numpy as np
            searcher.purge_all()
            return np.stack([_fake_vec() for _ in texts])

        searcher._encode_batch = _encode_batch_then_purge

        result = searcher.index_all(
            [{"id": "a", "text": "один"}, {"id": "b", "text": "два"}]
        )
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(result.get("reason"), "purged_during_encode")
        with searcher._index_lock:
            self.assertEqual(searcher._index, [])
            self.assertIsNone(searcher._embeddings)
        self.assertFalse(searcher._embeddings_path.exists())
        self.assertFalse(searcher._index_path.exists())


class TestMaxItemsEviction(unittest.TestCase):
    """LOW: SEMANTIC_SEARCH_MAX_ITEMS caps the index with FIFO eviction."""

    def test_index_item_evicts_oldest_over_cap(self):
        tmpdir = tempfile.mkdtemp()
        searcher = _make_searcher(tmpdir, max_items=3)
        searcher._encode = lambda model, text: _fake_vec()

        for i in range(6):
            self.assertTrue(searcher.index_item(f"id{i}", f"текст {i}"))

        with searcher._index_lock:
            # Only the most-recent 3 survive; oldest (id0, id1, id2) evicted.
            self.assertEqual(searcher._index, ["id3", "id4", "id5"])
            self.assertEqual(searcher._embeddings.shape[0], 3)

    def test_index_all_respects_cap(self):
        tmpdir = tempfile.mkdtemp()
        searcher = _make_searcher(tmpdir, max_items=2)
        import numpy as np
        searcher._encode_batch = lambda model, texts: np.stack(
            [_fake_vec() for _ in texts]
        )
        items = [{"id": f"i{n}", "text": f"t{n}"} for n in range(5)]
        searcher.index_all(items)
        with searcher._index_lock:
            self.assertEqual(len(searcher._index), 2)
            self.assertEqual(searcher._index, ["i3", "i4"])
            self.assertEqual(searcher._embeddings.shape[0], 2)

    def test_cap_zero_is_unbounded(self):
        tmpdir = tempfile.mkdtemp()
        searcher = _make_searcher(tmpdir, max_items=0)
        searcher._encode = lambda model, text: _fake_vec()
        for i in range(10):
            searcher.index_item(f"id{i}", f"t{i}")
        with searcher._index_lock:
            self.assertEqual(len(searcher._index), 10)

    def test_invalid_cap_falls_back_to_unbounded(self):
        tmpdir = tempfile.mkdtemp()
        searcher = SemanticSearcher(
            data_dir=Path(tmpdir),
            model_name="test-model",
            enabled=True,
            max_items="not-an-int",  # type: ignore[arg-type]
        )
        self.assertEqual(searcher._max_items, 0)


class TestSaveFailSurfacedToErrorBus(unittest.TestCase):
    """LOW: _save_locked persistence failure is surfaced via _error_bus."""

    def test_save_failure_pushes_error(self):
        tmpdir = tempfile.mkdtemp()
        searcher = _make_searcher(tmpdir)
        searcher._encode = lambda model, text: _fake_vec()

        pushed = []

        class _FakeBus:
            def push(self, err):
                pushed.append(err)

        searcher._error_bus = _FakeBus()

        # Force _save_locked to raise — monkeypatch np.save to blow up. We patch
        # the module-level numpy import path used inside _save_locked.
        import numpy as np
        orig_save = np.save

        def _boom(*a, **k):
            raise OSError("disk full (simulated)")

        np.save = _boom
        try:
            ok = searcher.index_item("id1", "текст")
        finally:
            np.save = orig_save

        # index_item swallows the save error (returns True), but the error bus
        # must have received a loud error so the divergence is not silent.
        self.assertTrue(ok)
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0].code, "history.write_fail")

    def test_save_failure_no_bus_is_silent(self):
        """When no _error_bus is wired, save failure stays a silent no-op (warning
        only) — no crash."""
        tmpdir = tempfile.mkdtemp()
        searcher = _make_searcher(tmpdir)
        searcher._encode = lambda model, text: _fake_vec()
        self.assertIsNone(searcher._error_bus)

        import numpy as np
        orig_save = np.save

        def _boom(*a, **k):
            raise OSError("disk full (simulated)")

        np.save = _boom
        try:
            ok = searcher.index_item("id1", "текст")  # must not raise
        finally:
            np.save = orig_save
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
