"""test_smart_vocab_suggestions_privacy.py — W973 privacy_mode gate.

Verifies that _handle_get_smart_vocabulary_suggestions returns an empty
suggestions list (not the real history-backed result) when privacy_mode_enabled
is True, preventing transcription history leakage in privacy mode.
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

from backend.service import BackendService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_svc(privacy_mode_enabled: bool) -> BackendService:
    """Minimal BackendService stub for _handle_get_smart_vocabulary_suggestions."""
    svc = BackendService.__new__(BackendService)

    # _get_runtime_setting delegates to _cached_settings()
    settings_dict: dict[str, Any] = {
        "privacy_mode_enabled": privacy_mode_enabled,
    }
    svc._cached_settings = lambda: settings_dict

    # store.get_history_page — should NOT be called in privacy mode
    store = MagicMock()
    store.get_history_page.return_value = ([], None)
    svc.store = store

    # vocabulary.load — should NOT be called in privacy mode
    vocabulary = MagicMock()
    vocabulary.load.return_value = []
    svc.vocabulary = vocabulary

    # _smart_vocabulary — should NOT be called in privacy mode
    smart_vocabulary = MagicMock()
    smart_vocabulary.get_vocabulary_suggestions.return_value = []
    svc._smart_vocabulary = smart_vocabulary

    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSmartVocabSuggestionsPrivacyGate(unittest.TestCase):
    """privacy_mode gate for _handle_get_smart_vocabulary_suggestions."""

    def test_smart_vocab_suggestions_empty_in_privacy_mode(self) -> None:
        """When privacy_mode_enabled=True, handler returns empty list and
        never touches history store or vocabulary."""
        svc = _make_svc(privacy_mode_enabled=True)

        result = svc._handle_get_smart_vocabulary_suggestions({})

        # Must return an ok response with empty suggestions
        self.assertEqual(result.get("suggestions"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok"), "Expected ok=True in privacy response")

        # History store must NOT have been accessed
        svc.store.get_history_page.assert_not_called()
        # Vocabulary must NOT have been accessed
        svc.vocabulary.load.assert_not_called()
        # SmartVocabularyBuilder must NOT have been called
        svc._smart_vocabulary.get_vocabulary_suggestions.assert_not_called()

    def test_smart_vocab_suggestions_runs_normally_without_privacy_mode(self) -> None:
        """When privacy_mode_enabled=False, handler proceeds with history scan."""
        svc = _make_svc(privacy_mode_enabled=False)
        # Provide a non-empty result so we can confirm the path ran
        svc._smart_vocabulary.get_vocabulary_suggestions.return_value = ["Krab", "Whisper"]

        result = svc._handle_get_smart_vocabulary_suggestions({"top_k": 10})

        # Handler should have called the store
        svc.store.get_history_page.assert_called_once()
        # vocabulary.load should have been called
        svc.vocabulary.load.assert_called_once()
        # SmartVocabularyBuilder should have been called
        svc._smart_vocabulary.get_vocabulary_suggestions.assert_called_once()

        self.assertEqual(result.get("suggestions"), ["Krab", "Whisper"])
        self.assertEqual(result.get("total"), 2)
        # 'reason' should NOT be present in the normal (non-privacy) response
        self.assertNotIn("reason", result)

    def test_privacy_mode_false_by_default_when_key_absent(self) -> None:
        """If privacy_mode_enabled is absent from settings, handler proceeds normally."""
        svc = _make_svc(privacy_mode_enabled=False)
        # Simulate missing key by overriding cached settings
        svc._cached_settings = lambda: {}

        result = svc._handle_get_smart_vocabulary_suggestions({})

        # No privacy gate should have fired — handler must have run normally
        svc.store.get_history_page.assert_called_once()


if __name__ == "__main__":
    unittest.main()
