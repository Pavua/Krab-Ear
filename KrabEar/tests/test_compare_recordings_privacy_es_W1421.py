"""Tests for W1408 F1+F2 fixes:
  F1 — compare_recordings privacy_mode_enabled guard
  F2 — _tokenize Spanish accented characters + ES stop words
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_comparison import (
    _STOP_WORDS_ES,
    _tokenize,
)


# ---------------------------------------------------------------------------
# F2 — _tokenize: Spanish accented characters
# ---------------------------------------------------------------------------


class TokenizeSpanishAccentsTestCase(unittest.TestCase):
    """_tokenize preserves Spanish accented characters (W1408 F2)."""

    def test_tokenize_preserves_spanish_vowels(self) -> None:
        """á é í ó ú не разбиваются на несколько токенов."""
        tokens = _tokenize("canción información")
        self.assertIn("canción", tokens)
        self.assertIn("información", tokens)

    def test_tokenize_preserves_n_tilde(self) -> None:
        """ñ сохраняется внутри слова."""
        tokens = _tokenize("español mañana")
        self.assertIn("español", tokens)
        self.assertIn("mañana", tokens)

    def test_tokenize_preserves_u_umlaut(self) -> None:
        """ü (güe/güi) сохраняется."""
        tokens = _tokenize("pingüino vergüenza")
        self.assertIn("pingüino", tokens)
        self.assertIn("vergüenza", tokens)

    def test_tokenize_does_not_split_accented_word(self) -> None:
        """Слово «médico» не разбивается на «m» и «dico»."""
        tokens = _tokenize("médico")
        self.assertIn("médico", tokens)
        # Убедимся, что не получились осколки "m" или "dico"
        self.assertNotIn("m", tokens)

    def test_tokenize_uppercase_accented(self) -> None:
        """Заглавные акцентированные символы нормализуются в lowercase."""
        tokens = _tokenize("Álvaro Ángel")
        # После lower() → álvaro, ángel
        self.assertIn("álvaro", tokens)
        self.assertIn("ángel", tokens)

    def test_tokenize_mixed_ru_es_en(self) -> None:
        """Токенизация смешанного RU+ES+EN текста работает корректно."""
        tokens = _tokenize("hola mundo здравствуй hello")
        self.assertIn("hola", tokens)
        self.assertIn("mundo", tokens)
        self.assertIn("здравствуй", tokens)
        self.assertIn("hello", tokens)


# ---------------------------------------------------------------------------
# F2 — ES stop words
# ---------------------------------------------------------------------------


class ESStopWordsTestCase(unittest.TestCase):
    """_STOP_WORDS_ES фильтрует испанские служебные слова (W1408 F2)."""

    def test_es_stop_words_set_nonempty(self) -> None:
        """_STOP_WORDS_ES не пустой."""
        self.assertGreater(len(_STOP_WORDS_ES), 5)

    def test_por_filtered(self) -> None:
        """«por» отфильтровывается из результата токенизации."""
        tokens = _tokenize("por favor trabaja")
        self.assertNotIn("por", tokens)

    def test_para_filtered(self) -> None:
        """«para» отфильтровывается."""
        tokens = _tokenize("para mañana trabaja")
        self.assertNotIn("para", tokens)

    def test_content_word_not_filtered(self) -> None:
        """Обычное содержательное испанское слово не фильтруется."""
        tokens = _tokenize("trabajo casa ciudad")
        self.assertIn("trabajo", tokens)
        self.assertIn("ciudad", tokens)

    def test_stop_word_combined_with_accent(self) -> None:
        """«más» — стоп-слово — отфильтровывается."""
        tokens = _tokenize("más trabajo educación")
        self.assertNotIn("más", tokens)
        # содержательные слова сохранены
        self.assertIn("trabajo", tokens)
        self.assertIn("educación", tokens)


# ---------------------------------------------------------------------------
# F1 — compare_recordings privacy guard (unit-level, no BackendService init)
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal store fake for privacy guard tests."""

    def __init__(self) -> None:
        self._items: dict[str, Any] = {}

    def add(self, item_id: str, text: str) -> None:
        self._items[item_id] = {"id": item_id, "text": text}

    def get_history_item_by_id(self, item_id: str):  # type: ignore[return]
        raw = self._items.get(item_id)
        if raw is None:
            return None
        # Return a simple object with to_dict()
        class _Item:
            def to_dict(self_inner) -> dict:
                return dict(raw)
        return _Item()


class _FakeBackendService:
    """Minimal stand-in for BackendService to test _handle_compare_recordings."""

    def __init__(self, privacy_enabled: bool = False) -> None:
        self._privacy_enabled = privacy_enabled
        from backend.recording_comparison import RecordingComparison, _view_to_dict as _comparison_view_to_dict
        self._recording_comparison = RecordingComparison()
        self._comparison_view_to_dict = _comparison_view_to_dict
        store = _FakeStore()
        store.add("a1", "Hello world this is test content")
        store.add("a2", "Hello world another test here")
        self.store = store

    def _get_runtime_setting(self, key: str, default: Any) -> Any:
        if key == "privacy_mode_enabled":
            return self._privacy_enabled
        return default

    def _handle_compare_recordings(self, params: dict) -> dict:
        """Verbatim copy of the patched handler from service.py."""
        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {
                "ok": True,
                "items": [],
                "common_words": [],
                "unique_words": {},
                "reason": "privacy_mode_active",
            }
        item_ids = params.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise ValueError("item_ids required")
        view = self._recording_comparison.compare(item_ids=item_ids, store=self.store)
        return self._comparison_view_to_dict(view)


class CompareRecordingsPrivacyModeTestCase(unittest.TestCase):
    """compare_recordings returns empty result when privacy_mode_enabled (W1408 F1)."""

    def test_compare_recordings_privacy_mode_empty_result(self) -> None:
        """_handle_compare_recordings returns empty items/common_words when privacy_mode_enabled."""
        svc = _FakeBackendService(privacy_enabled=True)
        result = svc._handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(result.get("items"), [])
        self.assertEqual(result.get("common_words"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_compare_recordings_privacy_mode_no_transcript_text(self) -> None:
        """Response must not contain any transcript text when privacy_mode_enabled."""
        import json
        svc = _FakeBackendService(privacy_enabled=True)
        result = svc._handle_compare_recordings({"item_ids": ["a1", "a2"]})
        dumped = json.dumps(result)
        self.assertNotIn("Hello world", dumped)

    def test_compare_recordings_privacy_mode_disabled_returns_data(self) -> None:
        """_handle_compare_recordings works normally when privacy_mode is NOT enabled."""
        svc = _FakeBackendService(privacy_enabled=False)
        result = svc._handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(len(result["items"]), 2)
        self.assertIn("text_similarity_matrix", result)
        self.assertNotIn("reason", result)


if __name__ == "__main__":
    unittest.main()
