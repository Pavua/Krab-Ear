"""Тесты W1541: restore _FILLER_STARTERS + _starts_with_filler (W1294 regression).

W1538 scanner found auto_glossary.py missing _starts_with_filler and
_FILLER_STARTERS — W1294 filler bigram filter reverted by W1497 cherry-pick train.

Tests:
  - test_filler_starters_constant_present
  - test_starts_with_filler_known_word_returns_true
  - test_starts_with_filler_normal_word_returns_false
  - test_filler_bigrams_filtered_from_glossary
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

# --- path setup ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.auto_glossary import (
    AutoGlossaryBuilder,
    _FILLER_STARTERS,
    _starts_with_filler,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_item(text: str) -> dict:
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {"text": text, "source_text": text, "ts": now_ts}


class _FakeStore:
    def __init__(self, items=None):
        self._items = items or []

    def get_history_page(self, cursor=None, limit=500):
        return self._items, None


# ── W1541 Tests ───────────────────────────────────────────────────────────────

class TestFillerStartersConstantPresent(unittest.TestCase):
    """_FILLER_STARTERS must exist and be a non-empty frozenset."""

    def test_filler_starters_constant_present(self):
        self.assertIsInstance(_FILLER_STARTERS, frozenset)
        self.assertGreater(len(_FILLER_STARTERS), 0)

    def test_filler_starters_contains_russian_fillers(self):
        for word in ("ну", "вот", "хорошо", "ладно", "значит", "давайте"):
            self.assertIn(word, _FILLER_STARTERS, f"Expected '{word}' in _FILLER_STARTERS")

    def test_filler_starters_contains_english_fillers(self):
        for word in ("well", "okay", "ok", "so"):
            self.assertIn(word, _FILLER_STARTERS, f"Expected '{word}' in _FILLER_STARTERS")

    def test_filler_starters_contains_spanish_fillers(self):
        for word in ("bueno", "pues", "vale"):
            self.assertIn(word, _FILLER_STARTERS, f"Expected '{word}' in _FILLER_STARTERS")


class TestStartsWithFillerKnownWordReturnsTrue(unittest.TestCase):
    """_starts_with_filler returns True for phrases starting with known fillers."""

    def test_starts_with_filler_known_word_returns_true(self):
        cases = [
            "ну хорошо",
            "хорошо давайте",
            "знаешь что",
            "ладно тогда",
            "давайте продолжим",
            "well then",
            "okay so",
            "ok let",
            "bueno pues",
            "vale entonces",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    _starts_with_filler(phrase),
                    f"Expected _starts_with_filler({phrase!r}) == True",
                )

    def test_starts_with_filler_case_insensitive(self):
        self.assertTrue(_starts_with_filler("ХОРОШО давайте"))
        self.assertTrue(_starts_with_filler("OKAY well"))
        self.assertTrue(_starts_with_filler("Хорошо Давайте"))

    def test_starts_with_filler_strips_punctuation(self):
        self.assertTrue(_starts_with_filler("ну, посмотрим"))
        self.assertTrue(_starts_with_filler("ok. let"))


class TestStartsWithFillerNormalWordReturnsFalse(unittest.TestCase):
    """_starts_with_filler returns False for non-filler phrases."""

    def test_starts_with_filler_normal_word_returns_false(self):
        cases = [
            "TensorFlow PyTorch",
            "Иван Иванов",
            "Apple Watch",
            "Яндекс Карты",
            "API Gateway",
            "HTTP POST",
            "Python Django",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertFalse(
                    _starts_with_filler(phrase),
                    f"Expected _starts_with_filler({phrase!r}) == False",
                )

    def test_starts_with_filler_empty_returns_false(self):
        self.assertFalse(_starts_with_filler(""))

    def test_starts_with_filler_single_word_normal(self):
        self.assertFalse(_starts_with_filler("TensorFlow"))
        self.assertFalse(_starts_with_filler("Москва"))


class TestFillerBigramsFilteredFromGlossary(unittest.TestCase):
    """build() must not return bigrams starting with filler words."""

    def _build_with(self, phrase: str, count: int = 6) -> list:
        items = [_make_item(phrase) for _ in range(count)]
        builder = AutoGlossaryBuilder(store=_FakeStore(items=items))
        return builder.build()

    def test_filler_bigrams_filtered_from_glossary(self):
        """'хорошо давайте' style bigrams must not appear in build() result."""
        phrase = "Хорошо давайте продолжим разговор о проекте хорошо давайте"
        result = self._build_with(phrase, count=6)
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(
                    first,
                    _FILLER_STARTERS,
                    f"Filler bigram leaked into glossary: {term!r}",
                )

    def test_znayesh_chto_filtered(self):
        """'знаешь что' must not appear in glossary."""
        phrase = "знаешь что я думаю знаешь что нам нужно"
        result = self._build_with(phrase, count=6)
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(first, _FILLER_STARTERS,
                                 f"Filler bigram leaked: {term!r}")

    def test_nu_i_filtered(self):
        """'ну и' must not appear in glossary."""
        phrase = "ну и ладно ну и хорошо ну и отлично"
        result = self._build_with(phrase, count=6)
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(first, _FILLER_STARTERS,
                                 f"Filler bigram leaked: {term!r}")

    def test_legitimate_terms_survive_filter(self):
        """TensorFlow, proper nouns survive the filler filter."""
        phrase = "TensorFlow PyTorch используются в одном проекте TensorFlow PyTorch"
        result = self._build_with(phrase, count=6)
        # No filler bigrams should leak
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(first, _FILLER_STARTERS,
                                 f"Filler bigram leaked: {term!r}")
        # TensorFlow should still be present as a single term
        self.assertTrue(
            any("TensorFlow" in t for t in result),
            f"TensorFlow should survive filter; got: {result}",
        )

    def test_all_filler_starters_produce_no_bigrams(self):
        """Bigrams starting with any _FILLER_STARTERS word are filtered."""
        words = " ".join(f"{f.capitalize()} слово" for f in _FILLER_STARTERS)
        items = [_make_item(words)] * 6
        builder = AutoGlossaryBuilder(store=_FakeStore(items=items))
        result = builder.build()
        for term in result:
            if " " in term:
                first = term.split()[0].lower()
                self.assertNotIn(
                    first,
                    _FILLER_STARTERS,
                    f"Filler bigram leaked into glossary: {term!r}",
                )


if __name__ == "__main__":
    unittest.main()
