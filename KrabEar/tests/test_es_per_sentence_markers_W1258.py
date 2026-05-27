"""W1258 — PunctuationFixer ES per-sentence ¿/¡ prepend tests.

Verifies that _fix_spanish prepends ¿/¡ only to the individual sentence
that ends with ?/!, not to the entire text (W1250 F1 MED regression fix).

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest \
        KrabEar/tests/test_es_per_sentence_markers_W1258.py -v
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.punctuation_fixer import PunctuationFixer  # noqa: E402


class TestEsMultiSentenceMarkersW1258(unittest.TestCase):
    """Per-sentence ¿/¡ insertion — W1250 F1 MED regression tests."""

    def setUp(self):
        self.fixer = PunctuationFixer()

    # ------------------------------------------------------------------ #
    # test_es_multi_sentence_question_marker_per_sentence                  #
    # ------------------------------------------------------------------ #
    def test_es_multi_sentence_question_marker_per_sentence(self):
        """'Hola. cómo estás?' → 'Hola. ¿Cómo estás?' (¿ only on last sentence)."""
        result = self.fixer.fix("Hola. cómo estás?", language="es")
        # ¿ must appear AFTER the first sentence separator, not at position 0
        self.assertNotEqual(result[0], "¿",
                            f"¿ must NOT be prepended to entire text: {result!r}")
        self.assertIn("¿", result,
                      f"¿ must appear somewhere in result: {result!r}")
        # The greeting sentence must be intact
        self.assertIn("Hola", result,
                      f"'Hola' sentence must be preserved: {result!r}")
        # ¿ must come after the period separator
        iquest_pos = result.index("¿")
        hola_pos = result.index("Hola")
        self.assertGreater(iquest_pos, hola_pos,
                           f"¿ must appear after 'Hola': {result!r}")

    # ------------------------------------------------------------------ #
    # test_es_multi_sentence_exclamation_marker_per_sentence               #
    # ------------------------------------------------------------------ #
    def test_es_multi_sentence_exclamation_marker_per_sentence(self):
        """'Buenos días. qué sorpresa!' → ¡ only on second sentence."""
        result = self.fixer.fix("Buenos días. qué sorpresa!", language="es")
        self.assertNotEqual(result[0], "¡",
                            f"¡ must NOT be prepended to entire text: {result!r}")
        self.assertIn("¡", result,
                      f"¡ must appear in result: {result!r}")
        self.assertIn("Buenos", result)
        iexcl_pos = result.index("¡")
        buenos_pos = result.index("Buenos")
        self.assertGreater(iexcl_pos, buenos_pos,
                           f"¡ must appear after 'Buenos': {result!r}")

    # ------------------------------------------------------------------ #
    # test_es_mixed_question_exclamation_per_sentence                      #
    # ------------------------------------------------------------------ #
    def test_es_mixed_question_exclamation_per_sentence(self):
        """'cómo estás? muy bien!' → ¿ on first sentence, ¡ on second."""
        result = self.fixer.fix("cómo estás? muy bien!", language="es")
        self.assertIn("¿", result, f"¿ expected: {result!r}")
        self.assertIn("¡", result, f"¡ expected: {result!r}")
        # ¿ must precede ¡ in the output
        self.assertLess(result.index("¿"), result.index("¡"),
                        f"¿ must come before ¡: {result!r}")

    # ------------------------------------------------------------------ #
    # test_es_existing_inverted_markers_not_duplicated                     #
    # ------------------------------------------------------------------ #
    def test_es_existing_inverted_markers_not_duplicated(self):
        """'¿cómo estás? Hola. ¡qué bueno!' — existing markers not doubled."""
        result = self.fixer.fix("¿cómo estás? Hola. ¡qué bueno!", language="es")
        self.assertNotIn("¿¿", result,
                         f"Double ¿¿ must not appear: {result!r}")
        self.assertNotIn("¡¡", result,
                         f"Double ¡¡ must not appear: {result!r}")

    # ------------------------------------------------------------------ #
    # test_es_single_question_still_works                                  #
    # ------------------------------------------------------------------ #
    def test_es_single_question_still_works(self):
        """Single-sentence question 'cómo estás?' → '¿Cómo estás?'."""
        result = self.fixer.fix("cómo estás?", language="es")
        self.assertTrue(result.lstrip().startswith("¿"),
                        f"Single question must start with ¿: {result!r}")
        self.assertNotIn("¿¿", result)

    def test_es_single_exclamation_still_works(self):
        """Single-sentence exclamation 'qué bueno!' → '¡Qué bueno!'."""
        result = self.fixer.fix("qué bueno!", language="es")
        self.assertTrue(result.lstrip().startswith("¡"),
                        f"Single exclamation must start with ¡: {result!r}")
        self.assertNotIn("¡¡", result)

    def test_es_declarative_sentence_unchanged(self):
        """Plain declarative 'Hola. Buenos días.' gets no ¿/¡."""
        result = self.fixer.fix("Hola. Buenos días.", language="es")
        self.assertNotIn("¿", result,
                         f"No ¿ expected for declarative: {result!r}")
        self.assertNotIn("¡", result,
                         f"No ¡ expected for declarative: {result!r}")

    def test_es_three_sentences_only_question_marked(self):
        """'Está bien. cómo te llamas. te llamas Juan?' — ¿ only on third."""
        result = self.fixer.fix("Está bien. cómo te llamas. te llamas Juan?",
                                language="es")
        self.assertIn("¿", result)
        # Count occurrences — should be exactly one ¿
        self.assertEqual(result.count("¿"), 1,
                         f"Expected exactly one ¿ but got: {result!r}")


if __name__ == "__main__":
    unittest.main()
