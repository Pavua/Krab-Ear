"""Wave 1455 — _apply_glossary lambda fix: backslash-safe replacement.

Regression tests for W1447 F1 HIGH: re.sub() interprets backslash sequences
in the replacement string, causing re.error for values like "C:\\Users" or "\\1ref".
Fix: wrap target in lambda so it is passed verbatim without escape interpretation.

Tests:
  - test_glossary_target_with_backslash_safe     — "C:\\Users" target, no re.error
  - test_glossary_target_with_group_ref_safe      — "\\1ref" target, no re.error
  - test_glossary_target_normal_string_works      — regression: plain string still works
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator  # noqa: E402


class ApplyGlossaryLambdaTestCase(unittest.TestCase):
    """Verifies that _apply_glossary handles backslash-containing targets safely."""

    def test_glossary_target_with_backslash_safe(self) -> None:
        """Target value 'C:\\Users' must not raise re.error (W1447 F1 HIGH)."""
        text = "The path is Users on this machine."
        glossary = {"Users": "C:\\Users"}
        # Must not raise re.error due to backslash interpretation
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "The path is C:\\Users on this machine.")

    def test_glossary_target_with_group_ref_safe(self) -> None:
        """Target value '\\1ref' must not raise re.error (group ref in replacement)."""
        text = "Call the function ref here."
        glossary = {"ref": "\\1ref"}
        # Without the lambda fix, re.sub("...ref...", "\\1ref", text) raises
        # re.error: invalid group reference 1 at position 1
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "Call the function \\1ref here.")

    def test_glossary_target_normal_string_works(self) -> None:
        """Regression: ordinary (no backslash) targets still replaced correctly."""
        text = "Translate AI into Spanish."
        glossary = {"AI": "Inteligencia Artificial"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "Translate Inteligencia Artificial into Spanish.")

    def test_glossary_target_double_backslash_path(self) -> None:
        """Windows-style full path as target value is preserved verbatim."""
        text = "Save to Documents please."
        glossary = {"Documents": "C:\\Users\\Admin\\Documents"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "Save to C:\\Users\\Admin\\Documents please.")

    def test_glossary_target_multiple_entries_with_backslash(self) -> None:
        """Multiple glossary entries including one with backslash all apply correctly."""
        text = "See Users and Temp folder."
        glossary = {
            "Users": "C:\\Users",
            "Temp": "C:\\Temp",
        }
        result = Translator._apply_glossary(text, glossary)
        self.assertIn("C:\\Users", result)
        self.assertIn("C:\\Temp", result)
        self.assertNotIn(" Users ", result)
        self.assertNotIn(" Temp ", result)

    def test_empty_glossary_passthrough(self) -> None:
        """Empty glossary returns text unchanged."""
        text = "Hello world"
        result = Translator._apply_glossary(text, {})
        self.assertEqual(result, "Hello world")

    def test_glossary_case_insensitive_match(self) -> None:
        """re.IGNORECASE flag: lowercase source matches uppercase in text."""
        text = "Apple is a fruit."
        glossary = {"apple": "Manzana"}
        result = Translator._apply_glossary(text, glossary)
        self.assertEqual(result, "Manzana is a fruit.")


if __name__ == "__main__":
    unittest.main()
