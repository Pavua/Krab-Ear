"""Tests for wave-29 privacy gates:
  C1 — LLMOpsService.handle_get_last_llm_diff blocks under privacy mode
  C2 — LLMOpsService.handle_replace_word_in_last_transcript blocks under privacy mode
  C3 — HistoryService.handle_word_frequency_analysis blocks under privacy mode
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_ops_service import LLMOpsService
from backend.history_service import HistoryService
from backend.state_store import StateStore


def _make_settings_svc(privacy_on: bool) -> MagicMock:
    svc = MagicMock()
    svc.cached_settings.return_value = {"privacy_mode_enabled": privacy_on}
    return svc


def _make_llm_ops(privacy_on: bool) -> LLMOpsService:
    settings_svc = _make_settings_svc(privacy_on)
    transcriber = MagicMock()
    transcriber.engine._last_llm_diff = None
    return LLMOpsService(store=None, settings_svc=settings_svc, transcriber=transcriber)


# ---------------------------------------------------------------------------
# C1 — get_last_llm_diff
# ---------------------------------------------------------------------------

class TestGetLastLlmDiffPrivacyGate(unittest.TestCase):

    def test_privacy_on_returns_blocked(self) -> None:
        svc = _make_llm_ops(privacy_on=True)
        result = svc.handle_get_last_llm_diff({})
        self.assertEqual(result.get("diff"), None)
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok"), "ok должен быть True при privacy блокировке")

    def test_privacy_on_does_not_touch_transcriber(self) -> None:
        svc = _make_llm_ops(privacy_on=True)
        # engine.whatever should NOT be accessed
        svc._transcriber.engine = MagicMock(side_effect=Exception("should not be called"))
        # Must not raise
        result = svc.handle_get_last_llm_diff({})
        self.assertIsNone(result.get("diff"))

    def test_privacy_off_no_diff_available(self) -> None:
        svc = _make_llm_ops(privacy_on=False)
        # _last_llm_diff is None → available=False
        result = svc.handle_get_last_llm_diff({})
        self.assertFalse(result.get("available"))
        self.assertIsNone(result.get("diff"))
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_off_with_diff_returns_data(self) -> None:
        svc = _make_llm_ops(privacy_on=False)
        fake_diff = MagicMock()
        fake_diff.similarity_ratio = 0.9
        fake_diff.words_added = ["новый"]
        fake_diff.words_removed = ["старый"]
        fake_diff.words_unchanged = ["слово"]
        fake_diff.summary = "1 replacement"
        fake_diff.changes = []
        svc._transcriber.engine._last_llm_diff = fake_diff
        result = svc.handle_get_last_llm_diff({})
        self.assertTrue(result.get("available"))
        self.assertIsNotNone(result.get("diff"))
        self.assertEqual(result["diff"]["similarity_ratio"], 0.9)


# ---------------------------------------------------------------------------
# C2 — replace_word_in_last_transcript
# ---------------------------------------------------------------------------

class TestReplaceWordPrivacyGate(unittest.TestCase):

    def _make_svc_with_store(self, privacy_on: bool) -> tuple:
        tmp = tempfile.TemporaryDirectory()
        store = StateStore(Path(tmp.name) / "data")
        settings_svc = _make_settings_svc(privacy_on)
        svc = LLMOpsService(store=store, settings_svc=settings_svc, transcriber=None)
        return svc, store, tmp

    def test_privacy_on_returns_blocked(self) -> None:
        svc, _, tmp = self._make_svc_with_store(privacy_on=True)
        try:
            result = svc.handle_replace_word_in_last_transcript(
                {"old_word": "hello", "new_word": "world"}
            )
            self.assertFalse(result.get("ok"), "ok должен быть False при privacy блокировке")
            self.assertEqual(result.get("reason"), "privacy_mode_active")
        finally:
            tmp.cleanup()

    def test_privacy_on_does_not_touch_store(self) -> None:
        svc, store, tmp = self._make_svc_with_store(privacy_on=True)
        try:
            # Add item — it should NOT be read/updated
            store.add_history_item(text="hello world test", paste_status="ok")
            result = svc.handle_replace_word_in_last_transcript(
                {"old_word": "hello", "new_word": "bye"}
            )
            self.assertEqual(result.get("reason"), "privacy_mode_active")
            # Confirm store not modified: load and check text unchanged
            with store._lock():
                items = store._load_active_items_unlocked()
            self.assertEqual(items[-1].text, "hello world test")
        finally:
            tmp.cleanup()

    def test_privacy_off_missing_words_error(self) -> None:
        svc, _, tmp = self._make_svc_with_store(privacy_on=False)
        try:
            result = svc.handle_replace_word_in_last_transcript(
                {"old_word": "", "new_word": "world"}
            )
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("error"), "missing_words")
        finally:
            tmp.cleanup()

    def test_privacy_off_replaces_word(self) -> None:
        svc, store, tmp = self._make_svc_with_store(privacy_on=False)
        try:
            store.add_history_item(text="привет мир", paste_status="ok")
            result = svc.handle_replace_word_in_last_transcript(
                {"old_word": "мир", "new_word": "земля"}
            )
            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("replaced_count"), 1)
            self.assertIn("земля", result.get("new_text", ""))
        finally:
            tmp.cleanup()


# ---------------------------------------------------------------------------
# C3 — word_frequency_analysis
# ---------------------------------------------------------------------------

class TestWordFrequencyPrivacyGate(unittest.TestCase):

    def _make_svc(self, privacy_on: bool) -> HistoryService:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        store.add_history_item(text="кот собака птица кот кот", paste_status="ok")
        cached_settings = lambda: {"privacy_mode_enabled": privacy_on}  # noqa: E731
        return HistoryService(store=store, cached_settings=cached_settings)

    def test_privacy_on_returns_empty_lists(self) -> None:
        svc = self._make_svc(privacy_on=True)
        result = svc.handle_word_frequency_analysis({})
        self.assertEqual(result.get("top_words"), [])
        self.assertEqual(result.get("bigrams"), [])
        self.assertEqual(result.get("words"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok"), "ok должен быть True при privacy блокировке")

    def test_privacy_on_total_words_zero(self) -> None:
        svc = self._make_svc(privacy_on=True)
        result = svc.handle_word_frequency_analysis({})
        self.assertEqual(result.get("total_words"), 0)
        self.assertEqual(result.get("unique_words"), 0)
        self.assertEqual(result.get("vocabulary_richness"), 0.0)
        self.assertEqual(result.get("by_language"), {})

    def test_privacy_off_returns_word_data(self) -> None:
        svc = self._make_svc(privacy_on=False)
        result = svc.handle_word_frequency_analysis({})
        top = result.get("top_words", [])
        self.assertTrue(len(top) > 0, "privacy off должен вернуть непустой top_words")
        words = [e["word"] for e in top]
        # "кот" appears 3 times; should be present
        self.assertIn("кот", words)

    def test_privacy_off_no_reason_field(self) -> None:
        svc = self._make_svc(privacy_on=False)
        result = svc.handle_word_frequency_analysis({})
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_on_with_no_cached_settings_fn(self) -> None:
        """Если cached_settings не передан — gate не срабатывает, история доступна."""
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        store = StateStore(Path(self.tmp2.name) / "data")
        store.add_history_item(text="слово слово", paste_status="ok")
        svc = HistoryService(store=store, cached_settings=None)
        result = svc.handle_word_frequency_analysis({})
        # No gate with None settings fn — should return real data
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main()
