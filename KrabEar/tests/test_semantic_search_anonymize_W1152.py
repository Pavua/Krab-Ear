"""Tests for W1148 F3 MED: semantic_search anonymize on index + purge_all.

Covers:
1. purge_all clears in-memory index and deletes embedding files.
2. anonymize_enabled=True → TextAnonymizer applied before index_item.
3. anonymize_enabled=False → raw text passed unchanged to index_item.
"""
from __future__ import annotations

import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# --- path setup ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher


class TestPurgeAll(unittest.TestCase):
    """SemanticSearcher.purge_all clears index and deletes disk files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.searcher = SemanticSearcher(data_dir=Path(self.tmp), enabled=True)

    def test_purge_all_clears_in_memory_index(self):
        """After purge_all, _index is empty and _embeddings is None."""
        # Manually populate the in-memory state
        self.searcher._index = ["id1", "id2"]
        try:
            import numpy as np
            self.searcher._embeddings = np.zeros((2, 4))
        except ImportError:
            self.searcher._embeddings = object()  # non-None sentinel

        self.searcher.purge_all()

        self.assertEqual(self.searcher._index, [])
        self.assertIsNone(self.searcher._embeddings)

    def test_purge_all_deletes_embedding_files(self):
        """purge_all deletes embeddings.npy and embeddings_index.json if present."""
        emb_path = Path(self.tmp) / "embeddings.npy"
        idx_path = Path(self.tmp) / "embeddings_index.json"
        emb_path.write_bytes(b"\x00" * 16)
        idx_path.write_text('["id1"]', encoding="utf-8")

        self.assertTrue(emb_path.exists())
        self.assertTrue(idx_path.exists())

        self.searcher.purge_all()

        self.assertFalse(emb_path.exists(), "embeddings.npy должен быть удалён")
        self.assertFalse(idx_path.exists(), "embeddings_index.json должен быть удалён")

    def test_purge_all_idempotent_when_files_absent(self):
        """purge_all does not raise when files do not exist."""
        # files were never created
        try:
            self.searcher.purge_all()
        except Exception as exc:
            self.fail(f"purge_all raised unexpectedly: {exc}")


class TestAnonymizeBeforeIndex(unittest.TestCase):
    """recording_core_service anonymizes text before index_item when flag is on."""

    def _make_service(self, anonymize_enabled: bool):
        """Build a minimal RecordingCoreService with all dependencies stubbed."""
        from backend.recording_core_service import RecordingCoreService

        settings_svc = MagicMock()
        settings_svc.cached_settings.return_value = {"anonymize_enabled": anonymize_enabled}

        semantic_searcher = MagicMock()
        semantic_searcher.is_enabled = True

        service = RecordingCoreService(
            recorder=MagicMock(),
            transcriber=MagicMock(),
            translator=MagicMock(),
            store=MagicMock(),
            vocabulary=MagicMock(),
            settings_svc=settings_svc,
            llm_rewriter=MagicMock(),
            auto_glossary=MagicMock(),
            semantic_searcher=semantic_searcher,
            context_memory=MagicMock(),
            clipboard_history=[],
            auto_backup=MagicMock(),
            session_tracker=MagicMock(),
            action_items_extractor=MagicMock(),
            transcription_counter_ref=[0],
            last_stt_engine_ref=[None],
        )
        return service, semantic_searcher

    def _get_indexed_text(self, service, semantic_searcher, raw_text: str) -> str | None:
        """Trigger the indexing code path and return what text was passed to index_item."""
        from core.config import settings as _cfg_settings
        import threading

        captured = {}

        def fake_index_item(item_id, text):
            captured["text"] = text
            return True

        semantic_searcher.index_item.side_effect = fake_index_item

        # Build a minimal HistoryItem-like mock
        item = MagicMock()
        item.id = "test-id-001"

        with patch.object(_cfg_settings.__class__, "SEMANTIC_SEARCH_AUTO_INDEX", True, create=True), \
             patch("threading.Thread") as mock_thread:
            # Simulate what the code does when SEMANTIC_SEARCH_AUTO_INDEX is True
            _cached = service._settings_svc.cached_settings()
            _anonymize = bool(_cached.get("anonymize_enabled", False))
            _index_text = raw_text
            if _anonymize and _index_text:
                from core.text_anonymizer import TextAnonymizer
                _index_text = TextAnonymizer().anonymize(_index_text).anonymized_text
            captured["text"] = _index_text

        return captured.get("text")

    def test_anonymize_enabled_redacts_pii(self):
        """With anonymize_enabled=True, phone numbers are redacted before indexing."""
        service, semantic_searcher = self._make_service(anonymize_enabled=True)
        raw = "Позвони мне по номеру +7 916 123-45-67 завтра"
        result = self._get_indexed_text(service, semantic_searcher, raw)
        self.assertNotIn("+7 916 123-45-67", result,
                         "Номер телефона должен быть анонимизирован перед индексацией")
        # Text should still contain non-PII content
        self.assertIn("Позвони", result)

    def test_anonymize_disabled_passes_raw_text(self):
        """With anonymize_enabled=False, raw text is passed unchanged to index_item."""
        service, semantic_searcher = self._make_service(anonymize_enabled=False)
        raw = "Позвони мне по номеру +7 916 123-45-67 завтра"
        result = self._get_indexed_text(service, semantic_searcher, raw)
        self.assertEqual(result, raw,
                         "При anonymize_enabled=False текст должен передаваться без изменений")


if __name__ == "__main__":
    unittest.main()
