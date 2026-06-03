"""test_wave29_translation_privacy_gates.py — wave-29 HIGH privacy gates.

Verifies that three handlers return empty results (no history access) when
privacy_mode_enabled=True, and run normally when privacy_mode_enabled=False:

  A1. TranslationService.handle_get_vocabulary_suggestions
  A2. TranslationService.handle_get_glossary_suggestions
  A3. GlossaryAutoLearnService.handle_suggest_medical_glossary_terms
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_service import TranslationService  # noqa: E402
from backend.glossary_auto_learn import GlossaryAutoLearnService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_translation_svc(
    privacy_mode_enabled: bool,
    history_items: list[dict] | None = None,
    vocabulary: list[str] | None = None,
) -> tuple[TranslationService, MagicMock]:
    """Build a minimal TranslationService with mocked store."""
    settings: dict[str, Any] = {
        "privacy_mode_enabled": privacy_mode_enabled,
        "translation_glossary": {},
    }
    store = MagicMock()
    store.get_history_page.return_value = (history_items or [], None)
    store.load_vocabulary.return_value = vocabulary or []

    translator = MagicMock()
    svc = TranslationService(
        translator=translator,
        store=store,
        cached_settings=lambda: dict(settings),
        invalidate_settings_cache=lambda: None,
    )
    return svc, store


def _make_glossary_auto_learn_svc(
    privacy_mode_enabled: bool,
    history_items: list[dict] | None = None,
) -> tuple[GlossaryAutoLearnService, MagicMock]:
    """Build a minimal GlossaryAutoLearnService with mocked store."""
    settings: dict[str, Any] = {
        "privacy_mode_enabled": privacy_mode_enabled,
        "translation_glossary": {},
    }
    store = MagicMock()
    store.get_history_page.return_value = (history_items or [], None)

    svc = GlossaryAutoLearnService(
        store=store,
        cached_settings=lambda: dict(settings),
        invalidate_settings_cache=lambda: None,
    )
    return svc, store


# Rich history items used in the "privacy OFF → returns data" tests
_VOCAB_HISTORY = [
    {"text": "Krab транскрибирует аудио"},
    {"text": "Krab транскрибирует аудио"},
    {"text": "Krab транскрибирует аудио"},
]

_GLOSSARY_HISTORY = [
    {
        "source_text": "Доктор назначил лечение",
        "translated_text": "Doctor prescribed tratamiento",
    },
    {
        "source_text": "Доктор назначил лечение",
        "translated_text": "Doctor prescribed tratamiento",
    },
]

_MEDICAL_HISTORY = [
    {
        "source_text": "пациент принимает антибиотик лекарство",
        "translated_text": "paciente toma antibiótico medicamento",
    },
    {
        "source_text": "пациент принимает антибиотик лекарство",
        "translated_text": "paciente toma antibiótico medicamento",
    },
]


# ---------------------------------------------------------------------------
# A1: handle_get_vocabulary_suggestions
# ---------------------------------------------------------------------------

class TestVocabularySuggestionsPrivacyGate(unittest.TestCase):
    """handle_get_vocabulary_suggestions — privacy gate (wave-29 A1)."""

    def test_privacy_on_returns_empty_no_history_access(self) -> None:
        """privacy_mode_enabled=True → empty suggestions, no history read."""
        svc, store = _make_translation_svc(
            privacy_mode_enabled=True,
            history_items=_VOCAB_HISTORY,
        )

        result = svc.handle_get_vocabulary_suggestions({})

        self.assertEqual(result.get("suggestions"), [])
        self.assertEqual(result.get("total"), 0)
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok"), "Expected ok=True in privacy response")
        store.get_history_page.assert_not_called()

    def test_privacy_on_no_vocabulary_store_access(self) -> None:
        """privacy_mode_enabled=True → vocabulary store not accessed."""
        svc, store = _make_translation_svc(privacy_mode_enabled=True)

        svc.handle_get_vocabulary_suggestions({})

        store.load_vocabulary.assert_not_called()

    def test_privacy_off_runs_normally(self) -> None:
        """privacy_mode_enabled=False → history is scanned, non-empty result possible."""
        svc, store = _make_translation_svc(
            privacy_mode_enabled=False,
            history_items=_VOCAB_HISTORY,
        )

        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 4})

        store.get_history_page.assert_called_once()
        self.assertIn("suggestions", result)
        self.assertNotIn("reason", result)

    def test_privacy_absent_from_settings_runs_normally(self) -> None:
        """When privacy_mode_enabled key is absent, gate does not fire."""
        settings: dict[str, Any] = {"translation_glossary": {}}
        store = MagicMock()
        store.get_history_page.return_value = ([], None)
        store.load_vocabulary.return_value = []

        svc = TranslationService(
            translator=MagicMock(),
            store=store,
            cached_settings=lambda: dict(settings),
            invalidate_settings_cache=lambda: None,
        )

        result = svc.handle_get_vocabulary_suggestions({})

        store.get_history_page.assert_called_once()
        self.assertNotIn("reason", result)


# ---------------------------------------------------------------------------
# A2: handle_get_glossary_suggestions
# ---------------------------------------------------------------------------

class TestGlossarySuggestionsPrivacyGate(unittest.TestCase):
    """handle_get_glossary_suggestions — privacy gate (wave-29 A2)."""

    def test_privacy_on_returns_empty_no_history_access(self) -> None:
        """privacy_mode_enabled=True → empty suggestions, no history read."""
        svc, store = _make_translation_svc(
            privacy_mode_enabled=True,
            history_items=_GLOSSARY_HISTORY,
        )

        result = svc.handle_get_glossary_suggestions({})

        self.assertEqual(result.get("suggestions"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok"), "Expected ok=True in privacy response")
        store.get_history_page.assert_not_called()

    def test_privacy_off_runs_normally(self) -> None:
        """privacy_mode_enabled=False → history is scanned."""
        svc, store = _make_translation_svc(
            privacy_mode_enabled=False,
            history_items=_GLOSSARY_HISTORY,
        )

        result = svc.handle_get_glossary_suggestions({"min_count": 2})

        store.get_history_page.assert_called_once()
        self.assertIn("suggestions", result)
        self.assertNotIn("reason", result)

    def test_privacy_absent_from_settings_runs_normally(self) -> None:
        """When privacy_mode_enabled key is absent, gate does not fire."""
        settings: dict[str, Any] = {"translation_glossary": {}}
        store = MagicMock()
        store.get_history_page.return_value = ([], None)

        svc = TranslationService(
            translator=MagicMock(),
            store=store,
            cached_settings=lambda: dict(settings),
            invalidate_settings_cache=lambda: None,
        )

        result = svc.handle_get_glossary_suggestions({})

        store.get_history_page.assert_called_once()
        self.assertNotIn("reason", result)

    def test_privacy_on_with_populated_history_still_returns_empty(self) -> None:
        """Even with rich translation history, privacy gate suppresses all data."""
        rich_items = [
            {
                "source_text": "Доктор назначил лечение Иванова",
                "translated_text": "Doctor prescribed tratamiento Ivanova",
            },
        ] * 5
        svc, store = _make_translation_svc(
            privacy_mode_enabled=True,
            history_items=rich_items,
        )

        result = svc.handle_get_glossary_suggestions({"min_count": 2, "top_k": 50})

        self.assertEqual(result.get("suggestions"), [])
        store.get_history_page.assert_not_called()


# ---------------------------------------------------------------------------
# A3: handle_suggest_medical_glossary_terms
# ---------------------------------------------------------------------------

class TestMedicalGlossaryPrivacyGate(unittest.TestCase):
    """handle_suggest_medical_glossary_terms — privacy gate (wave-29 A3)."""

    def test_privacy_on_returns_empty_no_history_access(self) -> None:
        """privacy_mode_enabled=True → empty suggestions, no history read."""
        svc, store = _make_glossary_auto_learn_svc(
            privacy_mode_enabled=True,
            history_items=_MEDICAL_HISTORY,
        )

        result = svc.handle_suggest_medical_glossary_terms({})

        self.assertEqual(result.get("suggestions"), [])
        store.get_history_page.assert_not_called()

    def test_privacy_off_runs_normally(self) -> None:
        """privacy_mode_enabled=False → history is scanned, handler works."""
        svc, store = _make_glossary_auto_learn_svc(
            privacy_mode_enabled=False,
            history_items=_MEDICAL_HISTORY,
        )

        result = svc.handle_suggest_medical_glossary_terms({"limit": 10})

        store.get_history_page.assert_called_once()
        self.assertIn("suggestions", result)
        self.assertIsInstance(result["suggestions"], list)

    def test_privacy_absent_from_settings_runs_normally(self) -> None:
        """When privacy_mode_enabled key is absent, gate does not fire."""
        settings: dict[str, Any] = {}  # no privacy_mode_enabled key
        store = MagicMock()
        store.get_history_page.return_value = ([], None)

        svc = GlossaryAutoLearnService(
            store=store,
            cached_settings=lambda: dict(settings),
            invalidate_settings_cache=lambda: None,
        )

        svc.handle_suggest_medical_glossary_terms({})

        store.get_history_page.assert_called_once()

    def test_privacy_on_with_medical_history_still_returns_empty(self) -> None:
        """Medical terms in translation history are fully suppressed in privacy mode."""
        medical_items = [
            {
                "source_text": "пациент принимает антибиотик антибиотик",
                "translated_text": "paciente toma antibiótico antibiótico",
            },
        ] * 5
        svc, store = _make_glossary_auto_learn_svc(
            privacy_mode_enabled=True,
            history_items=medical_items,
        )

        result = svc.handle_suggest_medical_glossary_terms({"limit": 20})

        self.assertEqual(result.get("suggestions"), [])
        store.get_history_page.assert_not_called()

    def test_privacy_on_limit_param_ignored(self) -> None:
        """When privacy mode is on, limit parameter is irrelevant — gate fires first."""
        svc, store = _make_glossary_auto_learn_svc(privacy_mode_enabled=True)

        result = svc.handle_suggest_medical_glossary_terms({"limit": 100})

        self.assertEqual(result.get("suggestions"), [])
        store.get_history_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
