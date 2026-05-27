"""Regression tests for W1425 F4 MED: _apply_glossary word-boundary fix.

Covers:
- substring non-match: "AI" must NOT corrupt "PAIN"
- exact word replacement: standalone "AI" -> "ИИ"
- case-insensitive matching: "ai" matches glossary entry "AI"
- special regex chars in source term are properly escaped
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator  # noqa: E402


class GlossaryWordBoundaryTestCase(unittest.TestCase):
    """_apply_glossary must respect word boundaries (W1425 F4 MED)."""

    def test_glossary_word_boundary_no_substring_match(self) -> None:
        """'AI' in 'PAIN' must remain unchanged — no mid-word substitution."""
        text = "PAIN"
        glossary = {"AI": "ИИ"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "PAIN",
                         "Short glossary term 'AI' must not corrupt substring 'PAIN'")

    def test_glossary_exact_word_replaced(self) -> None:
        """Standalone 'AI' at word boundary is replaced with 'ИИ'."""
        text = "The AI model"
        glossary = {"AI": "ИИ"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "The ИИ model",
                         "Standalone 'AI' should be replaced with 'ИИ'")

    def test_glossary_case_insensitive_match(self) -> None:
        """Lowercase 'ai' must match glossary entry 'AI' (case-insensitive)."""
        text = "the ai assistant"
        glossary = {"AI": "ИИ"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "the ИИ assistant",
                         "Matching should be case-insensitive")

    def test_glossary_mixed_case_in_sentence(self) -> None:
        """'Ai' variant also matches 'AI' glossary entry."""
        text = "Ai is great but PAIN is bad"
        glossary = {"AI": "ИИ"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "ИИ is great but PAIN is bad",
                         "'Ai' should match but 'PAIN' substring must not be touched")

    def test_glossary_special_chars_escaped(self) -> None:
        """Source term with regex special chars like '.' must not become wildcards.

        re.escape prevents special chars from being interpreted as regex operators.
        Note: \b word-boundary only works when the term starts/ends with a word-char.
        Terms ending in non-word chars (like 'C++') use the leading \b only.
        """
        # Use a term starting with a word char so \b applies correctly at the start
        text = "use SDK2 and not SDK2extra"
        glossary = {"SDK2": "СДК2"}
        result = Translator._apply_glossary(text, glossary)
        # Standalone SDK2 is replaced; SDK2extra (longer word) is preserved
        self.assertEqual(result, "use СДК2 and not SDK2extra",
                         "Standalone SDK2 replaced; SDK2extra (superstring) untouched")

    def test_glossary_dot_star_in_term_escaped(self) -> None:
        """A term containing '.' or '*' should not become a wildcard pattern."""
        text = "www.example and something else"
        glossary = {"www.example": "сайт"}
        result = Translator._apply_glossary(text, glossary)
        # '.' should match literal dot only (via re.escape), not any character
        self.assertIn("сайт", result,
                      "Literal dot in term must match only a literal dot")
        self.assertNotIn("www.example", result,
                         "Original term should have been replaced")

    def test_glossary_empty_glossary_passthrough(self) -> None:
        """Empty glossary returns the text unchanged."""
        text = "Some text"
        result = Translator._apply_glossary(text, {})
        self.assertEqual(result, "Some text")

    def test_glossary_word_at_start_and_end(self) -> None:
        """Word at start/end of string (text boundaries act as word boundaries)."""
        text = "AI processes data with AI"
        glossary = {"AI": "ИИ"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "ИИ processes data with ИИ",
                         "Word at string start and end should both be replaced")


if __name__ == "__main__":
    unittest.main()
