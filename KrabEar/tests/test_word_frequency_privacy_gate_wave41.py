"""Wave-41 regression tests: word_frequency_analysis dead privacy gate fix.

BUG (HIGH): handle_word_frequency_analysis used a dead inline check:
    if self._cached_settings is not None and self._cached_settings().get("privacy_mode_enabled"):
In production HistoryService is constructed with cached_settings=None in some code paths,
making the gate never fire — privacy mode had no effect, allowing transcript-derived
word/bigram data to leak.

FIX: replaced with self._is_privacy_mode(), which correctly falls back to
store.load_settings() when _cached_settings is None (same pattern as ~30 sibling handlers).

Tests:
- privacy mode ON  → empty words/bigrams, store NOT accessed (gate fires before load)
- privacy mode ON  → response schema matches sibling handlers (words/bigrams/by_language all [])
- privacy mode OFF → normal results returned (gate does NOT block)
- privacy mode ON with data seeded → store_load_active_items NOT called
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from backend.history_service import HistoryService
    from backend.state_store import StateStore
    _SKIP = False
except ImportError:
    _SKIP = True


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class WordFrequencyPrivacyGateTestCase(unittest.TestCase):
    """handle_word_frequency_analysis privacy gate must fire via _is_privacy_mode()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        # Construct WITHOUT passing cached_settings — this is the code path where the
        # dead inline gate `self._cached_settings is not None and ...` was always False.
        self.svc = HistoryService(store=self.store)

    # ------------------------------------------------------------------
    # A: privacy mode ON → empty response
    # ------------------------------------------------------------------
    def test_privacy_mode_on_returns_empty_words(self) -> None:
        """privacy_mode_enabled=True → words list is empty."""
        # Seed real data so a non-gated call would return content.
        self.store.add_history_item(text="секретный разговор с агентом", paste_status="ok")
        self.store.save_settings({"privacy_mode_enabled": True})

        result = self.svc.handle_word_frequency_analysis({})

        self.assertEqual(result.get("words", "NOT_PRESENT"), [],
                         "words must be [] in privacy mode")
        self.assertEqual(result.get("top_words", "NOT_PRESENT"), [],
                         "top_words must be [] in privacy mode")

    def test_privacy_mode_on_returns_empty_bigrams(self) -> None:
        """privacy_mode_enabled=True → bigrams list is empty."""
        self.store.add_history_item(text="первое второе третье первое второе", paste_status="ok")
        self.store.save_settings({"privacy_mode_enabled": True})

        result = self.svc.handle_word_frequency_analysis({})

        self.assertEqual(result.get("bigrams", "NOT_PRESENT"), [],
                         "bigrams must be [] in privacy mode")

    def test_privacy_mode_on_returns_empty_by_language(self) -> None:
        """privacy_mode_enabled=True → by_language dict is empty."""
        self.store.add_history_item(text="кот пёс лис", paste_status="ok", source_lang="ru")
        self.store.save_settings({"privacy_mode_enabled": True})

        result = self.svc.handle_word_frequency_analysis({})

        self.assertEqual(result.get("by_language", "NOT_PRESENT"), {},
                         "by_language must be {} in privacy mode")

    # ------------------------------------------------------------------
    # B: schema parity — response keys + reason field
    # ------------------------------------------------------------------
    def test_privacy_mode_on_schema_parity(self) -> None:
        """Response in privacy mode must contain all schema keys with empty/zero values."""
        self.store.save_settings({"privacy_mode_enabled": True})

        result = self.svc.handle_word_frequency_analysis({})

        self.assertTrue(result.get("ok", False), "ok must be True even in privacy mode")
        self.assertEqual(result.get("words"), [])
        self.assertEqual(result.get("bigrams"), [])
        self.assertEqual(result.get("top_words"), [])
        self.assertEqual(result.get("total_words"), 0)
        self.assertEqual(result.get("unique_words"), 0)
        self.assertAlmostEqual(result.get("vocabulary_richness", -1.0), 0.0)
        self.assertEqual(result.get("by_language"), {})
        self.assertEqual(result.get("reason"), "privacy_mode_active",
                         "reason must be privacy_mode_active")

    # ------------------------------------------------------------------
    # C: store NOT accessed when privacy mode is ON
    # ------------------------------------------------------------------
    def test_privacy_mode_on_store_not_accessed(self) -> None:
        """Gate must short-circuit before loading any history items from the store."""
        self.store.add_history_item(text="конфиденциальные данные", paste_status="ok")
        self.store.save_settings({"privacy_mode_enabled": True})

        with patch.object(
            self.store, "_load_active_items_unlocked",
            wraps=self.store._load_active_items_unlocked,
        ) as mock_load:
            self.svc.handle_word_frequency_analysis({})
            mock_load.assert_not_called()

    # ------------------------------------------------------------------
    # D: privacy mode OFF → gate does NOT block, normal results returned
    # ------------------------------------------------------------------
    def test_privacy_mode_off_returns_data(self) -> None:
        """privacy_mode_enabled=False → handler returns real word frequency data."""
        self.store.add_history_item(text="работа работа проект проект проект", paste_status="ok")
        self.store.save_settings({"privacy_mode_enabled": False})

        result = self.svc.handle_word_frequency_analysis({})

        self.assertNotEqual(result.get("reason"), "privacy_mode_active",
                            "gate must NOT fire when privacy mode is off")
        self.assertGreater(result.get("total_words", 0), 0,
                           "total_words must be > 0 when privacy mode is off")
        self.assertIsInstance(result.get("top_words"), list)
        self.assertGreater(len(result.get("top_words", [])), 0,
                           "top_words must be non-empty when privacy mode is off")

    # ------------------------------------------------------------------
    # E: _is_privacy_mode() works when cached_settings is None (regression path)
    # ------------------------------------------------------------------
    def test_gate_works_when_cached_settings_is_none(self) -> None:
        """_is_privacy_mode() must fire even when _cached_settings is None (uses store fallback)."""
        # Verify the service was constructed without cached_settings (the failing path).
        self.assertIsNone(self.svc._cached_settings,
                          "Service must have _cached_settings=None to test the regression")

        self.store.add_history_item(text="секретное слово", paste_status="ok")
        self.store.save_settings({"privacy_mode_enabled": True})

        result = self.svc.handle_word_frequency_analysis({})

        # If the OLD dead gate were still in place this would return real data.
        # The fix (_is_privacy_mode) correctly reads from store.load_settings().
        self.assertEqual(result.get("top_words"), [],
                         "Gate must fire even when _cached_settings is None")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    # ------------------------------------------------------------------
    # F: gate fires regardless of params (language filter / limit)
    # ------------------------------------------------------------------
    def test_privacy_mode_on_ignores_language_param(self) -> None:
        """Privacy gate fires before params are processed."""
        self.store.add_history_item(text="хорошо плохо", paste_status="ok", source_lang="ru")
        self.store.save_settings({"privacy_mode_enabled": True})

        result = self.svc.handle_word_frequency_analysis({"language": "ru"})

        self.assertEqual(result.get("top_words"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_on_ignores_limit_param(self) -> None:
        """Privacy gate fires before limit param is processed."""
        for i in range(10):
            self.store.add_history_item(text=f"слово{i} текст{i}", paste_status="ok")
        self.store.save_settings({"privacy_mode_enabled": True})

        result = self.svc.handle_word_frequency_analysis({"limit": 5})

        self.assertEqual(result.get("top_words"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main()
