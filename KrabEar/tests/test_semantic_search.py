"""Tests for SemanticSearcher and IPC handlers (semantic_search / status / reindex).

All tests are mock-based — no real sentence-transformers model is loaded.
8 test cases:
  1. index_item — adds embedding to index
  2. search — returns cosine-ranked results
  3. cosine_similarity — pure math
  4. fallback — keyword fallback when model unavailable
  5. disabled — all ops no-op when enabled=False
  6. IPC handlers — semantic_search / status / reindex via BackendService
  7. progress (index_all) — indexed/skipped/errors counts
  8. lazy_load — model loaded only on first use
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher, keyword_fallback_search


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_model(dim: int = 4):
    """Returns a MagicMock that mimics SentenceTransformer API."""
    import numpy as np
    model = MagicMock()

    def _encode(text, normalize_embeddings=True):
        # Deterministic hash-based embedding for reproducibility
        seed = sum(ord(c) for c in text) % 1000
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


class TestSemanticSearchIndexItem(unittest.TestCase):
    """Test 1: index_item adds embedding to the in-memory index."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        # Inject fake model
        self.fake_model = _make_fake_model()
        self.searcher._model = self.fake_model
        self.searcher._model_loaded = True

    def test_index_item_adds_entry(self):
        ok = self.searcher.index_item("id1", "Привет мир")
        self.assertTrue(ok)
        with self.searcher._index_lock:
            self.assertIn("id1", self.searcher._index)
            self.assertEqual(self.searcher._embeddings.shape[0], 1)

    def test_index_item_updates_existing(self):
        self.searcher.index_item("id1", "Первый текст")
        self.searcher.index_item("id1", "Обновлённый текст")
        with self.searcher._index_lock:
            self.assertEqual(self.searcher._index.count("id1"), 1)
            self.assertEqual(self.searcher._embeddings.shape[0], 1)

    def test_index_item_empty_text_skipped(self):
        ok = self.searcher.index_item("id2", "   ")
        self.assertFalse(ok)
        with self.searcher._index_lock:
            self.assertNotIn("id2", self.searcher._index)

    def test_index_item_disabled_returns_false(self):
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=False,
        )
        ok = searcher.index_item("id1", "Привет")
        self.assertFalse(ok)


class TestSemanticSearchSearch(unittest.TestCase):
    """Test 2: search returns cosine-ranked results."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        self.fake_model = _make_fake_model(dim=8)
        self.searcher._model = self.fake_model
        self.searcher._model_loaded = True

        # Pre-populate index
        for i in range(5):
            self.searcher.index_item(f"item{i}", f"Текст номер {i}")

    def test_search_returns_results(self):
        results = self.searcher.search("Текст номер 2", top_k=3)
        self.assertIsInstance(results, list)
        self.assertLessEqual(len(results), 3)
        for r in results:
            self.assertIn("id", r)
            self.assertIn("score", r)

    def test_search_score_range(self):
        results = self.searcher.search("запрос", top_k=5)
        for r in results:
            self.assertGreaterEqual(r["score"], -1.0)
            self.assertLessEqual(r["score"], 1.0 + 1e-6)

    def test_search_empty_query_returns_empty(self):
        results = self.searcher.search("  ", top_k=5)
        self.assertEqual(results, [])

    def test_search_top_k_respected(self):
        results = self.searcher.search("текст", top_k=2)
        self.assertLessEqual(len(results), 2)

    def test_search_disabled_returns_empty(self):
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=False,
        )
        results = searcher.search("запрос", top_k=5)
        self.assertEqual(results, [])


class TestCosineSimiliarity(unittest.TestCase):
    """Test 3: _cosine_similarity_batch correctness."""

    def test_identical_vectors(self):
        import numpy as np
        v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        matrix = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        scores = SemanticSearcher._cosine_similarity_batch(v, matrix)
        self.assertAlmostEqual(float(scores[0]), 1.0, places=5)
        self.assertAlmostEqual(float(scores[1]), 0.0, places=5)

    def test_orthogonal_vectors(self):
        import numpy as np
        v = np.array([1.0, 0.0], dtype=np.float32)
        matrix = np.array([[0.0, 1.0]], dtype=np.float32)
        scores = SemanticSearcher._cosine_similarity_batch(v, matrix)
        self.assertAlmostEqual(float(scores[0]), 0.0, places=5)

    def test_opposite_vectors(self):
        import numpy as np
        v = np.array([1.0, 0.0], dtype=np.float32)
        matrix = np.array([[-1.0, 0.0]], dtype=np.float32)
        scores = SemanticSearcher._cosine_similarity_batch(v, matrix)
        self.assertAlmostEqual(float(scores[0]), -1.0, places=5)


class TestKeywordFallback(unittest.TestCase):
    """Test 4: keyword_fallback_search when model unavailable."""

    def test_fallback_finds_matches(self):
        items = [
            {"id": "a", "text": "Привет мир"},
            {"id": "b", "text": "Как дела"},
            {"id": "c", "text": "Привет снова"},
        ]
        results = keyword_fallback_search("привет", items, top_k=10)
        ids = [r["id"] for r in results]
        self.assertIn("a", ids)
        self.assertIn("c", ids)
        self.assertNotIn("b", ids)

    def test_fallback_top_k_respected(self):
        items = [{"id": str(i), "text": f"текст {i}"} for i in range(20)]
        results = keyword_fallback_search("текст", items, top_k=5)
        self.assertLessEqual(len(results), 5)

    def test_fallback_empty_query(self):
        items = [{"id": "a", "text": "Привет"}]
        results = keyword_fallback_search("", items, top_k=5)
        self.assertEqual(results, [])

    def test_fallback_score_sorting(self):
        items = [
            {"id": "a", "text": "один два три"},
            {"id": "b", "text": "один два"},
            {"id": "c", "text": "один"},
        ]
        results = keyword_fallback_search("один два три", items, top_k=10)
        self.assertEqual(results[0]["id"], "a")

    def test_fallback_model_unavailable_path(self):
        """SemanticSearcher falls back to keyword when model_error is set."""
        tmpdir = tempfile.mkdtemp()
        searcher = SemanticSearcher(
            data_dir=Path(tmpdir),
            model_name="nonexistent-model",
            enabled=True,
        )
        searcher._model_error = "model_unavailable"
        # search should return [] (keyword fallback is caller's responsibility)
        results = searcher.search("запрос", top_k=5)
        self.assertEqual(results, [])


class TestSemanticSearchDisabled(unittest.TestCase):
    """Test 5: all operations are no-op when enabled=False."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=False,
        )

    def test_status_shows_disabled(self):
        s = self.searcher.status()
        self.assertFalse(s["enabled"])

    def test_index_item_returns_false(self):
        ok = self.searcher.index_item("id1", "text")
        self.assertFalse(ok)

    def test_search_returns_empty(self):
        results = self.searcher.search("query", top_k=5)
        self.assertEqual(results, [])

    def test_index_all_returns_disabled_reason(self):
        result = self.searcher.index_all([{"id": "x", "text": "hello"}])
        self.assertEqual(result.get("reason"), "disabled")

    def test_model_not_loaded(self):
        self.assertFalse(self.searcher.model_loaded)


class TestSemanticSearchIPC(unittest.TestCase):
    """Test 6: IPC handler methods in BackendService."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Build a minimal BackendService with a real SemanticSearcher (disabled)
        from backend.state_store import StateStore
        self.store = StateStore(data_dir=Path(self.tmpdir))

        # Patch heavy dependencies so BackendService.__init__ doesn't crash
        with patch("backend.service.AudioRecorder"), \
             patch("backend.service.Transcriber"), \
             patch("backend.service.Translator"), \
             patch("backend.service.AutoBackupManager"):
            from backend.service import BackendService
            self.service = BackendService.__new__(BackendService)
            self.service.store = self.store
            # Inject minimal searcher
            self.searcher = SemanticSearcher(
                data_dir=Path(self.tmpdir),
                model_name="test-model",
                enabled=False,
            )
            self.service._semantic_searcher = self.searcher
            # Wave 757: inject SearchAndAnalysisService so delegation shims work
            from backend.search_and_analysis_service import SearchAndAnalysisService
            self.service._search_analysis_svc = SearchAndAnalysisService(
                store=self.store,
                semantic_searcher=self.searcher,
                action_items_extractor=None,
                topic_tracker=None,
                recording_insights=None,
                recording_comparison=None,
                stats_report=None,
            )

    def test_semantic_search_status_ipc(self):
        result = self.service._search_analysis_svc.handle_semantic_search_status({})
        self.assertIn("enabled", result)
        self.assertFalse(result["enabled"])
        self.assertIn("indexed_count", result)

    def test_semantic_search_disabled_returns_disabled(self):
        result = self.service._search_analysis_svc.handle_semantic_search({"query": "test"})
        self.assertEqual(result["mode"], "keyword")  # fallback

    def test_semantic_search_reindex_disabled(self):
        result = self.service._search_analysis_svc.handle_semantic_search_reindex({})
        self.assertEqual(result.get("reason"), "semantic_search_disabled")

    def test_semantic_search_empty_query_raises(self):
        with self.assertRaises(ValueError):
            self.service._search_analysis_svc.handle_semantic_search({"query": ""})

    def test_semantic_search_with_enabled_searcher(self):
        # Enable the searcher and inject a fake model
        self.searcher._enabled = True
        fake_model = _make_fake_model(dim=4)
        self.searcher._model = fake_model
        self.searcher._model_loaded = True
        self.searcher.index_item("item1", "Привет мир")
        result = self.service._search_analysis_svc.handle_semantic_search({"query": "привет", "top_k": 5})
        self.assertIn("results", result)
        self.assertEqual(result["mode"], "semantic")


class TestSemanticSearchProgress(unittest.TestCase):
    """Test 7: index_all returns accurate indexed/skipped/errors counts."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        self.fake_model = _make_fake_model(dim=4)
        self.searcher._model = self.fake_model
        self.searcher._model_loaded = True

    def test_index_all_counts(self):
        items = [
            {"id": "a", "text": "Первый"},
            {"id": "b", "text": "Второй"},
            {"id": "", "text": "Без id"},
            {"id": "c", "text": ""},
        ]
        result = self.searcher.index_all(items)
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["errors"], 0)

    def test_index_all_force_rebuild(self):
        items = [{"id": "a", "text": "Текст"}]
        self.searcher.index_all(items)
        # Second call without force should skip already indexed
        result = self.searcher.index_all(items, force=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["indexed"], 0)
        # Force rebuild
        result_force = self.searcher.index_all(items, force=True)
        self.assertEqual(result_force["indexed"], 1)

    def test_index_all_disabled_returns_disabled_reason(self):
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=False,
        )
        result = searcher.index_all([{"id": "x", "text": "hello"}])
        self.assertEqual(result["reason"], "disabled")


class TestSemanticSearchLazyLoad(unittest.TestCase):
    """Test 8: model is loaded only on first use, not at construction."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_model_not_loaded_at_construction(self):
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        self.assertFalse(searcher.model_loaded)
        self.assertIsNone(searcher._model)

    def test_model_load_called_on_first_index(self):
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        fake_model = _make_fake_model(dim=4)
        with patch("backend.semantic_search.SemanticSearcher._get_model",
                   return_value=fake_model) as mock_get:
            searcher.index_item("id1", "Привет")
            mock_get.assert_called_once()

    def test_model_load_called_on_search(self):
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        fake_model = _make_fake_model(dim=4)
        # Pre-populate without going through _get_model
        import numpy as np
        with searcher._index_lock:
            searcher._index = ["id1"]
            searcher._embeddings = np.random.rand(1, 4).astype(np.float32)

        with patch("backend.semantic_search.SemanticSearcher._get_model",
                   return_value=fake_model) as mock_get:
            searcher.search("запрос", top_k=5)
            mock_get.assert_called_once()

    def test_sentence_transformers_not_installed_graceful(self):
        """When sentence_transformers is absent, _get_model returns None gracefully."""
        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            # Simulate ImportError path by setting model_error directly
            searcher._model_error = "sentence_transformers_not_installed"
            model = searcher._get_model()
            self.assertIsNone(model)


class TestSemanticSearchWave137(unittest.TestCase):
    """Wave 137 additional tests: unicode, empty-index, remove, concurrent."""

    def _make_searcher(self, tmpdir):
        searcher = SemanticSearcher(
            data_dir=Path(tmpdir),
            model_name="test-model",
            enabled=True,
        )
        searcher._model = _make_fake_model(dim=8)
        searcher._model_loaded = True
        return searcher

    def test_unicode_query_text(self):
        """Unicode query (Cyrillic + Spanish) encodes without error."""
        tmpdir = tempfile.mkdtemp()
        searcher = self._make_searcher(tmpdir)
        searcher.index_item("ru1", "Привет мир — тест поиска")
        searcher.index_item("es1", "Hola mundo — búsqueda semántica")
        searcher.index_item("mixed", "Café résumé naïve")

        results = searcher.search("Привет búsqueda", top_k=3)
        self.assertIsInstance(results, list)
        for r in results:
            self.assertIn("id", r)
            self.assertIn("score", r)

    def test_empty_index_returns_empty(self):
        """search() on a freshly-created (empty) index returns []."""
        tmpdir = tempfile.mkdtemp()
        searcher = self._make_searcher(tmpdir)
        # No items indexed
        results = searcher.search("anything", top_k=5)
        self.assertEqual(results, [])

    def test_remove_doc_from_index(self):
        """Removing a document from the index excludes it from future searches."""
        import numpy as np
        tmpdir = tempfile.mkdtemp()
        searcher = self._make_searcher(tmpdir)
        searcher.index_item("keep", "Текст который остаётся в индексе")
        searcher.index_item("remove_me", "Документ для удаления из индекса")

        # Manually remove "remove_me" from the in-memory index (SemanticSearcher
        # has no public remove API — simulate by direct state manipulation, as
        # the class exposes _index / _embeddings for this purpose)
        with searcher._index_lock:
            if "remove_me" in searcher._index:
                idx = searcher._index.index("remove_me")
                searcher._index.pop(idx)
                searcher._embeddings = np.delete(searcher._embeddings, idx, axis=0)

        results = searcher.search("документ удаление", top_k=10)
        result_ids = [r["id"] for r in results]
        self.assertNotIn("remove_me", result_ids)
        self.assertIn("keep", result_ids)

    def test_concurrent_index_search(self):
        """Concurrent index + search calls do not raise exceptions."""
        import threading
        tmpdir = tempfile.mkdtemp()
        searcher = self._make_searcher(tmpdir)

        errors = []

        def do_index(n):
            try:
                for i in range(5):
                    searcher.index_item(f"thread{n}_item{i}", f"Текст {n} номер {i}")
            except Exception as exc:
                errors.append(exc)

        def do_search():
            try:
                for _ in range(5):
                    searcher.search("Текст номер", top_k=3)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_index, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=do_search) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")

    def test_handles_encoder_unavailable_gracefully(self):
        """When encoder is unavailable, search/index return safe empty values."""
        tmpdir = tempfile.mkdtemp()
        searcher = SemanticSearcher(
            data_dir=Path(tmpdir),
            model_name="nonexistent-model-xyz",
            enabled=True,
        )
        # Simulate model load failure
        searcher._model_error = "model_load_failed"
        searcher._model_loaded = False
        searcher._model = None

        ok = searcher.index_item("id1", "Текст для индексации")
        self.assertFalse(ok)

        results = searcher.search("запрос", top_k=5)
        self.assertEqual(results, [])

        status = searcher.status()
        self.assertEqual(status["model_error"], "model_load_failed")


if __name__ == "__main__":
    unittest.main()
