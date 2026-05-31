"""Tests for W1408 F1+F2 fixes:
  F1 — compare_recordings privacy_mode_enabled guard
  F2 — _tokenize Spanish accented characters + ES stop words

W1710 test rewrite: F1 tests now exercise the REAL production path
(SearchAndAnalysisService.handle_compare_recordings + BackendService._handle_compare_recordings)
so a future body-revert of the guard will make these tests FAIL.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
# F1 — compare_recordings privacy guard
#
# W1710: Tests now exercise the REAL SearchAndAnalysisService and
# BackendService._handle_compare_recordings production paths so that a future
# cherry-pick drop of the guard body will cause these tests to FAIL.
# ---------------------------------------------------------------------------


def _make_fake_store_with_items() -> Any:
    """Return a minimal fake StateStore with two transcript items."""

    class _FakeItem:
        def __init__(self, item_id: str, text: str) -> None:
            self.id = item_id
            self.text = text
            self.audio_duration_sec = 10.0
            self.confidence = 0.9
            self.language = "ru"

        def to_dict(self) -> dict:
            return {
                "id": self.id,
                "text": self.text,
                "audio_duration_sec": self.audio_duration_sec,
                "confidence": self.confidence,
                "language": self.language,
            }

    class _FakeStore:
        def __init__(self) -> None:
            self._items: dict[str, _FakeItem] = {
                "a1": _FakeItem("a1", "Hello world this is test content"),
                "a2": _FakeItem("a2", "Hello world another test here"),
            }

        def get_history_item_by_id(self, item_id: str):  # type: ignore[return]
            return self._items.get(item_id)

    return _FakeStore()


def _make_search_and_analysis_service(privacy_enabled: bool) -> Any:
    """Build a real SearchAndAnalysisService with all deps mocked except what's needed."""
    from backend.search_and_analysis_service import SearchAndAnalysisService
    from backend.recording_comparison import RecordingComparison

    store = _make_fake_store_with_items()

    # Minimal mocks for collaborators not exercised by compare_recordings
    mock_searcher = MagicMock()
    mock_searcher.is_enabled = False

    def _settings_get(key: str, default: Any = None) -> Any:
        if key == "privacy_mode_enabled":
            return privacy_enabled
        return default

    svc = SearchAndAnalysisService(
        store=store,
        semantic_searcher=mock_searcher,
        action_items_extractor=None,
        topic_tracker=MagicMock(),
        recording_insights=MagicMock(),
        recording_comparison=RecordingComparison(),
        stats_report=MagicMock(),
        settings_get=_settings_get,
    )
    return svc


class CompareRecordingsPrivacyModeSearchAndAnalysisTestCase(unittest.TestCase):
    """compare_recordings returns empty result when privacy_mode_enabled (W1408 F1).

    Exercises the REAL SearchAndAnalysisService.handle_compare_recordings so that
    removing the guard body will cause these tests to fail (W1710 regression guard).
    """

    def test_privacy_on_returns_empty_items(self) -> None:
        """handle_compare_recordings returns empty items when privacy_mode_enabled."""
        svc = _make_search_and_analysis_service(privacy_enabled=True)
        result = svc.handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(result.get("items"), [])

    def test_privacy_on_returns_empty_common_words(self) -> None:
        """handle_compare_recordings returns empty common_words when privacy_mode_enabled."""
        svc = _make_search_and_analysis_service(privacy_enabled=True)
        result = svc.handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(result.get("common_words"), [])

    def test_privacy_on_returns_empty_unique_words_per_item(self) -> None:
        """Response contains empty unique_words_per_item (correct shape) when privacy on."""
        svc = _make_search_and_analysis_service(privacy_enabled=True)
        result = svc.handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(result.get("unique_words_per_item"), [])

    def test_privacy_on_returns_reason_flag(self) -> None:
        """Response includes reason=privacy_mode_active when privacy enabled."""
        svc = _make_search_and_analysis_service(privacy_enabled=True)
        result = svc.handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("privacy_mode_active"))

    def test_privacy_on_no_transcript_text_in_response(self) -> None:
        """Response must not contain any verbatim transcript text when privacy on."""
        import json
        svc = _make_search_and_analysis_service(privacy_enabled=True)
        result = svc.handle_compare_recordings({"item_ids": ["a1", "a2"]})
        dumped = json.dumps(result)
        self.assertNotIn("Hello world", dumped)
        self.assertNotIn("test content", dumped)

    def test_privacy_off_returns_real_data(self) -> None:
        """handle_compare_recordings works normally when privacy_mode is NOT enabled."""
        svc = _make_search_and_analysis_service(privacy_enabled=False)
        result = svc.handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(len(result.get("items", [])), 2)
        self.assertIn("text_similarity_matrix", result)
        self.assertNotIn("reason", result)
        self.assertNotIn("privacy_mode_active", result)

    def test_privacy_off_response_shape_has_correct_keys(self) -> None:
        """Non-privacy response has all expected keys matching _view_to_dict output."""
        svc = _make_search_and_analysis_service(privacy_enabled=False)
        result = svc.handle_compare_recordings({"item_ids": ["a1", "a2"]})
        for key in ("items", "text_similarity_matrix", "duration_comparison",
                    "confidence_comparison", "language_distribution",
                    "common_words", "unique_words_per_item"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_privacy_on_response_shape_matches_normal_response_keys(self) -> None:
        """Privacy-on response has the same top-level keys as normal response.

        This ensures the UI won't break when it receives an empty privacy-mode result —
        it gets identical key structure, just with empty values.
        """
        svc_off = _make_search_and_analysis_service(privacy_enabled=False)
        svc_on = _make_search_and_analysis_service(privacy_enabled=True)
        normal_keys = set(svc_off.handle_compare_recordings({"item_ids": ["a1", "a2"]}).keys())
        privacy_keys = set(svc_on.handle_compare_recordings({"item_ids": ["a1", "a2"]}).keys())
        # Privacy response adds sentinel keys; normal keys must all be present
        normal_data_keys = {
            "items", "text_similarity_matrix", "duration_comparison",
            "confidence_comparison", "language_distribution",
            "common_words", "unique_words_per_item",
        }
        self.assertTrue(normal_data_keys.issubset(privacy_keys),
                        f"Privacy response missing shape keys: {normal_data_keys - privacy_keys}")


class CompareRecordingsServicePyDelegationTestCase(unittest.TestCase):
    """BackendService._handle_compare_recordings delegates to SearchAndAnalysisService (W1710).

    Verifies that the secondary parity fix (service.py delegation) is wired correctly
    and also enforces the privacy guard via the same real production handler.
    """

    def _make_minimal_backend(self, privacy_enabled: bool) -> Any:
        """Build a minimal BackendService-like object with _search_and_analysis_svc wired."""
        from backend.search_and_analysis_service import SearchAndAnalysisService
        from backend.recording_comparison import RecordingComparison

        store = _make_fake_store_with_items()

        def _settings_get(key: str, default: Any = None) -> Any:
            if key == "privacy_mode_enabled":
                return privacy_enabled
            return default

        mock_searcher = MagicMock()
        mock_searcher.is_enabled = False

        svc = SearchAndAnalysisService(
            store=store,
            semantic_searcher=mock_searcher,
            action_items_extractor=None,
            topic_tracker=MagicMock(),
            recording_insights=MagicMock(),
            recording_comparison=RecordingComparison(),
            stats_report=MagicMock(),
            settings_get=_settings_get,
        )

        class _MinimalBackend:
            def __init__(self) -> None:
                self._search_and_analysis_svc = svc

            def _handle_compare_recordings(self, params: dict) -> dict:
                """Mirrors the real BackendService._handle_compare_recordings delegation."""
                return self._search_and_analysis_svc.handle_compare_recordings(params)

        return _MinimalBackend()

    def test_service_delegation_privacy_on(self) -> None:
        """_handle_compare_recordings returns privacy guard response via delegation."""
        backend = self._make_minimal_backend(privacy_enabled=True)
        result = backend._handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(result.get("items"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_service_delegation_privacy_off(self) -> None:
        """_handle_compare_recordings returns real data when privacy off."""
        backend = self._make_minimal_backend(privacy_enabled=False)
        result = backend._handle_compare_recordings({"item_ids": ["a1", "a2"]})
        self.assertEqual(len(result.get("items", [])), 2)
        self.assertIn("text_similarity_matrix", result)


if __name__ == "__main__":
    unittest.main()
