"""Wave 935 — regression tests for glossary word-boundary fix (W926 F1 HIGH).

Verifies that _apply_glossary uses \b regex instead of str.replace so it
does NOT corrupt substrings:
  - "el"    must not touch "elecciones"
  - "дом"   must not touch "домой"
  - "привет" → "hi" must work for full-word standalone match
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator  # noqa: E402


class GlossaryWordBoundaryTests(unittest.TestCase):
    """W926 F1 HIGH: glossary substring corruption guard."""

    # ------------------------------------------------------------------
    # Spanish: "el" must not touch "elecciones"
    # ------------------------------------------------------------------
    def test_glossary_does_not_corrupt_es_substring(self):
        """Glossary entry 'el' must not rewrite substring in 'elecciones'."""
        glossary = {"el": "the"}
        text = "el resultado de las elecciones"
        result = Translator._apply_glossary(text, glossary)
        # "el " (standalone) at position 0 should be replaced → "the resultado…"
        # but "el" inside "elecciones" must stay untouched
        self.assertIn("elecciones", result,
                      "Substring 'elecciones' should survive glossary replacement of 'el'")
        self.assertNotIn("theciones", result,
                         "'theciones' must not appear — 'el' inside 'elecciones' was corrupted")

    # ------------------------------------------------------------------
    # Cyrillic: "дом" must not touch "домой"
    # ------------------------------------------------------------------
    def test_glossary_does_not_corrupt_ru_substring(self):
        """Glossary entry 'дом' must not rewrite the substring in 'домой'."""
        glossary = {"дом": "house"}
        text = "иди домой и зайди в дом"
        result = Translator._apply_glossary(text, glossary)
        # "дом" at the end (standalone after space) → "house"
        # but "дом" inside "домой" must stay untouched
        self.assertIn("домой", result,
                      "'домой' should survive glossary replacement of 'дом'")
        self.assertNotIn("houseой", result,
                         "'houseой' must not appear — 'дом' inside 'домой' was corrupted")
        self.assertIn("house", result,
                      "standalone 'дом' should still be replaced with 'house'")

    # ------------------------------------------------------------------
    # Basic: standalone word is replaced correctly
    # ------------------------------------------------------------------
    def test_glossary_replaces_standalone_word(self):
        """Standalone glossary source word is replaced with the target."""
        glossary = {"привет": "hi"}
        text = "привет мир"
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "hi мир",
                         "Standalone 'привет' must be replaced with 'hi'")

    # ------------------------------------------------------------------
    # Edge: empty glossary returns text unchanged
    # ------------------------------------------------------------------
    def test_empty_glossary_passthrough(self):
        """Empty glossary dict must return the original text unchanged."""
        text = "без изменений"
        result = Translator._apply_glossary(text, {})
        self.assertEqual(result, text)

    # ------------------------------------------------------------------
    # Edge: glossary entry that is at the very start and end of text
    # ------------------------------------------------------------------
    def test_glossary_replaces_at_boundaries(self):
        """Glossary entry at text start and end is replaced correctly."""
        glossary = {"cat": "gato"}
        text = "cat and cat"
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "gato and gato")


if __name__ == "__main__":
    unittest.main()
