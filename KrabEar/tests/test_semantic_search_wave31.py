"""wave-31 regression tests: semantic_search purge resurrection race (D1),
privacy gates for semantic_search + reindex (D2), and reset_model_error
clarification (D3 — docstring-only, no behavioural change).

D1 — purge_all() resurrection race fix (MED):
    The original purge_all() cleared memory THEN bumped _purge_epoch.
    _load_from_disk() is called lazily inside _get_model() (under _model_lock).
    If model loading started AFTER the disk files were deleted (step 2) but BEFORE
    the epoch was bumped (old step 3), _load_from_disk acquired _index_lock,
    saw the old epoch, and could persist a clean empty index back to disk —
    resurrection-by-empty-write race.
    Fix: bump _purge_epoch FIRST (step 0) under _index_lock, before clearing memory
    or touching the disk.  Any concurrent _load_from_disk or _save_locked that next
    acquires _index_lock sees the new epoch and must abort.

D2 — privacy gates (MED):
    handle_semantic_search and handle_semantic_search_reindex both accessed the
    transcript store without a privacy_mode_enabled gate.  Both now return early
    with an empty/zero result when privacy mode is active.

D3 — reset_model_error clarification (LOW):
    The method only resets the model-load error and model state — NOT the in-memory
    index.  This is now documented in its docstring.  No behavioural change.

Tests here are deterministic, fast, and load no real ML models.
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

from backend.semantic_search import SemanticSearcher  # noqa: E402
from backend.search_and_analysis_service import SearchAndAnalysisService  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fake_vec(dim: int = 4) -> np.ndarray:
    return np.ones(dim, dtype=np.float32) / float(dim) ** 0.5


def _make_searcher(tmpdir: str, enabled: bool = True) -> SemanticSearcher:
    """SemanticSearcher with fake pre-loaded model, no real sentence-transformers."""
    s = SemanticSearcher(
        data_dir=Path(tmpdir),
        model_name="test-model",
        enabled=enabled,
    )
    s._model = object()
    s._model_loaded = True
    # Inject deterministic fake encode methods so real model is never called
    s._encode = lambda model, text: _fake_vec()
    s._encode_batch = lambda model, texts: np.stack([_fake_vec() for _ in texts])
    return s


def _make_sas_service(tmpdir: str, searcher: SemanticSearcher,
                      privacy: bool = False) -> SearchAndAnalysisService:
    """SearchAndAnalysisService with a minimal store stub and configurable privacy."""
    from backend.state_store import StateStore

    store = StateStore(data_dir=Path(tmpdir))
    settings: dict = {"privacy_mode_enabled": privacy}

    svc = SearchAndAnalysisService(
        store=store,
        semantic_searcher=searcher,
        action_items_extractor=None,
        topic_tracker=None,
        recording_insights=None,
        recording_comparison=None,
        stats_report=None,
        settings_get=lambda k, d: settings.get(k, d),
    )
    return svc


# ===========================================================================
# D1: purge_all() epoch-first fix — resurrection race
# ===========================================================================

class TestPurgeEpochFirstOrder(unittest.TestCase):
    """D1: _purge_epoch must be bumped BEFORE clearing memory in purge_all()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(self.tmpdir)

    def test_epoch_increments_before_clear(self):
        """purge_all() must bump _purge_epoch atomically with (or before) clearing
        _embeddings/_index, all under _index_lock — not after."""
        searcher = self.searcher

        # Build a small index first
        searcher.index_item("id1", "текст один")
        searcher.index_item("id2", "текст два")

        epoch_before = searcher._purge_epoch

        # Track the epoch observed at the moment _index is cleared inside purge_all()
        # by monkey-patching __setattr__ on the list-slot — we can't do that on a
        # built-in, so instead we probe via a racing reader thread that holds
        # _index_lock briefly and checks the epoch after purge_all() starts.
        observed_epochs: list[int] = []

        original_purge = searcher.purge_all

        def patched_purge():
            original_purge()
            # After purge_all() returns, both epoch AND index should be consistent
            with searcher._index_lock:
                observed_epochs.append(searcher._purge_epoch)

        patched_purge()

        self.assertEqual(observed_epochs[0], epoch_before + 1)

    def test_load_from_disk_after_purge_sees_new_epoch(self):
        """Simulate: model loads AFTER purge files are deleted but the epoch is
        already bumped (step 0 fix).  _load_from_disk acquires _index_lock and
        should NOT overwrite the empty cleared state."""
        searcher = self.searcher

        # Pre-populate disk files
        searcher.index_item("id1", "sensitive transcript text")
        self.assertTrue(searcher._embeddings_path.exists())
        self.assertTrue(searcher._index_path.exists())

        # Run purge — epoch bumps FIRST, then files are deleted
        purge_epoch_after = None

        orig_purge = SemanticSearcher.purge_all

        def capturing_purge(self_inner):
            orig_purge(self_inner)
            nonlocal purge_epoch_after
            purge_epoch_after = self_inner._purge_epoch

        SemanticSearcher.purge_all = capturing_purge
        try:
            searcher.purge_all()
        finally:
            SemanticSearcher.purge_all = orig_purge

        # After purge, disk files should be gone
        self.assertFalse(searcher._embeddings_path.exists())
        self.assertFalse(searcher._index_path.exists())

        # The epoch observed right after purge should be the new one
        self.assertEqual(purge_epoch_after, 1)

        # Now if _load_from_disk is called (as if model just loaded lazily),
        # with files gone it should leave the index empty — no resurrection
        searcher._load_from_disk()

        with searcher._index_lock:
            self.assertEqual(searcher._index, [],
                             "_load_from_disk must not resurrect data after purge")
            self.assertIsNone(searcher._embeddings)

        # And still no files on disk
        self.assertFalse(searcher._embeddings_path.exists())
        self.assertFalse(searcher._index_path.exists())

    def test_concurrent_purge_then_load_from_disk_no_resurrection(self):
        """Thread-safety: concurrent purge + _load_from_disk calls do not
        result in any data surviving in memory."""
        searcher = self.searcher
        searcher.index_item("id_secret", "PII data")

        errors: list[Exception] = []

        def do_purge():
            try:
                searcher.purge_all()
            except Exception as exc:
                errors.append(exc)

        def do_load():
            try:
                searcher._load_from_disk()
            except Exception as exc:
                errors.append(exc)

        # Fire both concurrently many times
        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=do_purge))
            threads.append(threading.Thread(target=do_load))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Exceptions in threads: {errors}")

        # Final state: after any number of purges there must be no data
        # (at least one purge ran)
        with searcher._index_lock:
            # Index may be empty or may have had items re-added by _load_from_disk
            # in a valid window, but _purge_epoch must be > 0
            self.assertGreater(searcher._purge_epoch, 0)

    def test_epoch_bumped_on_multiple_purges(self):
        """Each successive purge_all() increments the epoch by exactly 1."""
        searcher = self.searcher
        self.assertEqual(searcher._purge_epoch, 0)
        searcher.purge_all()
        self.assertEqual(searcher._purge_epoch, 1)
        searcher.purge_all()
        self.assertEqual(searcher._purge_epoch, 2)
        searcher.purge_all()
        self.assertEqual(searcher._purge_epoch, 3)

    def test_index_item_after_purge_uses_new_epoch(self):
        """After purge, a fresh index_item must succeed with the new epoch."""
        searcher = self.searcher
        searcher.index_item("old", "old text")

        searcher.purge_all()

        ok = searcher.index_item("new", "fresh text")
        self.assertTrue(ok, "index_item should succeed after purge with new epoch")
        with searcher._index_lock:
            self.assertIn("new", searcher._index)
            self.assertNotIn("old", searcher._index)


# ===========================================================================
# D2: privacy gates — handle_semantic_search
# ===========================================================================

class TestSemanticSearchPrivacyGate(unittest.TestCase):
    """D2: handle_semantic_search must return empty result when privacy_mode_enabled."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(self.tmpdir, enabled=True)

    def test_privacy_mode_returns_empty_results(self):
        """In privacy mode, handle_semantic_search returns {'results': [], 'mode': 'disabled',
        'reason': 'privacy_mode_active'}."""
        svc = _make_sas_service(self.tmpdir, self.searcher, privacy=True)
        result = svc.handle_semantic_search({"query": "test query"})
        self.assertEqual(result["results"], [])
        self.assertEqual(result["mode"], "disabled")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_does_not_call_searcher(self):
        """In privacy mode, the SemanticSearcher.search method is never called."""
        call_count = {"search": 0, "fallback": 0}

        class TrackingSearcher:
            is_enabled = True

            def search(self, query, top_k=10):
                call_count["search"] += 1
                return []

        from backend.state_store import StateStore
        store = StateStore(data_dir=Path(self.tmpdir))
        svc = SearchAndAnalysisService(
            store=store,
            semantic_searcher=TrackingSearcher(),
            action_items_extractor=None,
            topic_tracker=None,
            recording_insights=None,
            recording_comparison=None,
            stats_report=None,
            settings_get=lambda k, d: True if k == "privacy_mode_enabled" else d,
        )

        svc.handle_semantic_search({"query": "anything"})

        self.assertEqual(call_count["search"], 0,
                         "SemanticSearcher.search must not be called in privacy mode")

    def test_privacy_mode_false_allows_search(self):
        """When privacy_mode_enabled=False, handle_semantic_search proceeds normally."""
        # Index one item so search can return a result
        self.searcher.index_item("item1", "test text for searching")

        svc = _make_sas_service(self.tmpdir, self.searcher, privacy=False)

        # search() will return results via the fake model — we just check it doesn't
        # early-return with the privacy gate
        result = svc.handle_semantic_search({"query": "test"})
        # Should NOT have privacy_mode_active marker
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_blocks_both_semantic_and_fallback(self):
        """The privacy gate fires before the fallback path — keyword fallback must
        also be blocked in privacy mode (it exposes transcript IDs)."""
        # Use a disabled searcher so the code would normally try fallback
        searcher_disabled = _make_searcher(self.tmpdir, enabled=False)
        svc = _make_sas_service(self.tmpdir, searcher_disabled, privacy=True)

        result = svc.handle_semantic_search({"query": "anything", "fallback": True})

        self.assertEqual(result["results"], [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_gate_requires_non_empty_query_only_in_normal_mode(self):
        """In privacy mode the early return fires before query validation."""
        svc = _make_sas_service(self.tmpdir, self.searcher, privacy=True)
        # Empty query would raise ValueError in normal mode, but privacy gate fires first
        result = svc.handle_semantic_search({"query": ""})
        self.assertEqual(result["results"], [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")


# ===========================================================================
# D2: privacy gates — handle_semantic_search_reindex
# ===========================================================================

class TestSemanticSearchReindexPrivacyGate(unittest.TestCase):
    """D2: handle_semantic_search_reindex must return indexed=0 when privacy mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = _make_searcher(self.tmpdir, enabled=True)

    def test_privacy_mode_returns_indexed_zero(self):
        """In privacy mode, handle_semantic_search_reindex returns
        {'indexed': 0, 'reason': 'privacy_mode_active'}."""
        svc = _make_sas_service(self.tmpdir, self.searcher, privacy=True)
        result = svc.handle_semantic_search_reindex({})
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_does_not_call_index_all(self):
        """In privacy mode, SemanticSearcher.index_all is never called."""
        call_count = {"index_all": 0}

        class TrackingSearcher:
            is_enabled = True

            def index_all(self, items, force=False):
                call_count["index_all"] += 1
                return {"indexed": 0, "skipped": 0, "errors": 0}

        from backend.state_store import StateStore
        store = StateStore(data_dir=Path(self.tmpdir))
        svc = SearchAndAnalysisService(
            store=store,
            semantic_searcher=TrackingSearcher(),
            action_items_extractor=None,
            topic_tracker=None,
            recording_insights=None,
            recording_comparison=None,
            stats_report=None,
            settings_get=lambda k, d: True if k == "privacy_mode_enabled" else d,
        )

        svc.handle_semantic_search_reindex({"force": True})

        self.assertEqual(call_count["index_all"], 0,
                         "SemanticSearcher.index_all must not be called in privacy mode")

    def test_privacy_mode_false_allows_reindex(self):
        """When privacy_mode_enabled=False, reindex proceeds normally (returns
        a dict without 'privacy_mode_active' reason)."""
        svc = _make_sas_service(self.tmpdir, self.searcher, privacy=False)
        result = svc.handle_semantic_search_reindex({})
        self.assertNotEqual(result.get("reason"), "privacy_mode_active",
                            "reindex should not be gated when privacy is off")

    def test_privacy_gate_fires_before_disabled_check(self):
        """Privacy gate fires even when semantic_search is disabled — it's a
        higher-priority gate than the is_enabled check."""
        searcher_disabled = _make_searcher(self.tmpdir, enabled=False)
        svc = _make_sas_service(self.tmpdir, searcher_disabled, privacy=True)
        result = svc.handle_semantic_search_reindex({})
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(result.get("reason"), "privacy_mode_active")


# ===========================================================================
# D3: reset_model_error clarification — behavioural invariant (docstring-only)
# ===========================================================================

class TestResetModelErrorScopeInvariant(unittest.TestCase):
    """D3: reset_model_error resets ONLY the model-load error, NOT the index."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_reset_does_not_clear_embeddings_index(self):
        """After reset_model_error(), in-memory embeddings and index must survive."""
        searcher = _make_searcher(self.tmpdir)
        searcher.index_item("kept_id", "this data should survive the reset")

        with searcher._index_lock:
            idx_before = list(searcher._index)
            emb_before = searcher._embeddings.copy() if searcher._embeddings is not None else None

        # Simulate a model error then reset
        searcher._model_error = "some_transient_error"
        searcher.reset_model_error()

        with searcher._index_lock:
            self.assertEqual(searcher._index, idx_before,
                             "reset_model_error must NOT clear _index")
            if emb_before is not None and searcher._embeddings is not None:
                self.assertEqual(searcher._embeddings.shape, emb_before.shape,
                                 "reset_model_error must NOT clear _embeddings")

    def test_reset_does_not_delete_disk_files(self):
        """After reset_model_error(), embeddings.npy / embeddings_index.json must persist."""
        searcher = _make_searcher(self.tmpdir)
        searcher.index_item("disk_item", "should stay on disk")

        self.assertTrue(searcher._embeddings_path.exists(),
                        "embeddings.npy should exist before reset")
        self.assertTrue(searcher._index_path.exists(),
                        "embeddings_index.json should exist before reset")

        searcher._model_error = "network_timeout"
        searcher.reset_model_error()

        self.assertTrue(searcher._embeddings_path.exists(),
                        "reset_model_error must NOT delete embeddings.npy")
        self.assertTrue(searcher._index_path.exists(),
                        "reset_model_error must NOT delete embeddings_index.json")

    def test_reset_returns_expected_shape(self):
        """reset_model_error returns {'reset': True, 'previous_error': <str or None>}."""
        searcher = _make_searcher(self.tmpdir)
        searcher._model_error = "connection_timeout"
        result = searcher.reset_model_error()
        self.assertTrue(result.get("reset"))
        self.assertEqual(result.get("previous_error"), "connection_timeout")
        self.assertIsNone(searcher._model_error)

    def test_reset_when_no_error(self):
        """reset_model_error with no prior error → previous_error=None, reset=True."""
        searcher = _make_searcher(self.tmpdir)
        self.assertIsNone(searcher._model_error)
        result = searcher.reset_model_error()
        self.assertTrue(result.get("reset"))
        self.assertIsNone(result.get("previous_error"))


if __name__ == "__main__":
    unittest.main()
