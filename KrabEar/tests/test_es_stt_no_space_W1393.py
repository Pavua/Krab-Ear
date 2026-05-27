"""W1393 — PunctuationFixer ES STT_PERIOD_NO_SPACE tests.

W1258 added per-sentence ¿/¡ insertion (splitting on .!?), but _NO_SPACE_AFTER_PERIOD_RE
only inserts a space before UPPERCASE letters (e.g. "Hola.Buenos" → "Hola. Buenos").
Whisper often outputs lowercase after a period without a space: "dime.como te llamas?".
In this case W1258 produced "Dime.¿como te llamas?" — ¿ correctly before "como" but the
period-run stays un-spaced, which looks broken.

W1393 adds _NO_SPACE_AFTER_SENT_LOWER_ES_RE applied *before* marker insertion in
_fix_spanish, which inserts a space so the result is "Dime. ¿como te llamas?".

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest \
        KrabEar/tests/test_es_stt_no_space_W1393.py -v
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.punctuation_fixer import PunctuationFixer  # noqa: E402


class TestEsSTTPeriodNoSpaceW1393(unittest.TestCase):
    """STT_PERIOD_NO_SPACE: ES ¿/¡ per-sentence with Whisper no-space output."""

    def setUp(self):
        self.fixer = PunctuationFixer()

    # ------------------------------------------------------------------ #
    # test_es_per_sentence_question                                         #
    # Required by W1374 F4: 'dime. como te llamas?' → ¿ only on second    #
    # ------------------------------------------------------------------ #
    def test_es_per_sentence_question(self):
        """'dime. como te llamas?' → '¿' before 'Como', not at start."""
        result = self.fixer.fix("dime. como te llamas?", language="es")
        self.assertNotEqual(result[0], "¿",
                            f"¿ must NOT be at position 0: {result!r}")
        self.assertIn("¿", result,
                      f"¿ must appear in result: {result!r}")
        # ¿ must come after 'Dime'
        iquest_pos = result.index("¿")
        dime_pos = result.index("Dime")
        self.assertGreater(iquest_pos, dime_pos,
                           f"¿ must appear after 'Dime': {result!r}")
        self.assertNotIn("¿¿", result, "No double ¿")

    # ------------------------------------------------------------------ #
    # test_es_per_sentence_no_space_after_period                           #
    # Core W1393 fix: Whisper outputs "dime.como" without space            #
    # ------------------------------------------------------------------ #
    def test_es_per_sentence_no_space_after_period(self):
        """'dime.como te llamas?' — STT no-space case: ¿ before 'como', space after period."""
        result = self.fixer.fix("dime.como te llamas?", language="es")
        # ¿ must NOT be at position 0 (would mean whole text prepended)
        self.assertNotEqual(result[0], "¿",
                            f"¿ must NOT be at position 0: {result!r}")
        self.assertIn("¿", result,
                      f"¿ must appear in result: {result!r}")
        # There must be a space separating the period from the next word
        self.assertNotIn(".¿", result,
                         f"Period must not be directly followed by ¿ (space required): {result!r}")
        self.assertNotIn(".c", result,
                         f"Period must not be directly followed by 'c' (space required): {result!r}")
        # The greeting must remain intact
        self.assertIn("Dime", result,
                      f"'Dime' sentence must be preserved: {result!r}")
        self.assertNotIn("¿¿", result, "No double ¿")

    # ------------------------------------------------------------------ #
    # test_es_per_sentence_no_space_uppercase_unaffected                   #
    # _NO_SPACE_AFTER_PERIOD_RE already covers uppercase; ensure no        #
    # double-space is introduced when both rules apply                     #
    # ------------------------------------------------------------------ #
    def test_es_per_sentence_no_space_uppercase_unaffected(self):
        """'dime.Como te llamas?' — uppercase after period: correct spacing, ¿ inserted."""
        result = self.fixer.fix("dime.Como te llamas?", language="es")
        self.assertNotEqual(result[0], "¿",
                            f"¿ must NOT be at position 0: {result!r}")
        self.assertIn("¿Como", result,
                      f"¿ must immediately precede 'Como': {result!r}")
        self.assertNotIn(".¿", result,
                         f"Period must not be directly followed by ¿: {result!r}")
        self.assertNotIn("  ", result, "No double spaces in output")

    # ------------------------------------------------------------------ #
    # test_es_per_sentence_idempotent                                       #
    # ------------------------------------------------------------------ #
    def test_es_per_sentence_idempotent(self):
        """Running fix twice must not change the result."""
        inp = "dime.como te llamas?"
        first = self.fixer.fix(inp, language="es")
        second = self.fixer.fix(first, language="es")
        self.assertEqual(first, second,
                         f"fix() must be idempotent:\n  1st: {first!r}\n  2nd: {second!r}")

    # ------------------------------------------------------------------ #
    # test_es_exclamation_no_space_after_period                            #
    # ------------------------------------------------------------------ #
    def test_es_exclamation_no_space_after_period(self):
        """'bien.qué suerte!' — ¡ before 'qué', space after period."""
        result = self.fixer.fix("bien.qué suerte!", language="es")
        self.assertNotEqual(result[0], "¡",
                            f"¡ must NOT be at position 0: {result!r}")
        self.assertIn("¡", result,
                      f"¡ must appear in result: {result!r}")
        self.assertNotIn(".¡", result,
                         f"Period must not be directly followed by ¡: {result!r}")
        self.assertNotIn("¡¡", result, "No double ¡")

    # ------------------------------------------------------------------ #
    # test_es_declarative_no_space_after_period — no ¿/¡ added             #
    # ------------------------------------------------------------------ #
    def test_es_declarative_no_space_after_period(self):
        """'hola.buenos días.' — declarative: space inserted but no ¿/¡."""
        result = self.fixer.fix("hola.buenos días.", language="es")
        self.assertNotIn("¿", result,
                         f"No ¿ expected for declarative: {result!r}")
        self.assertNotIn("¡", result,
                         f"No ¡ expected for declarative: {result!r}")
        # Space must be inserted between sentences
        self.assertNotIn(".buenos", result,
                         f"Space must be inserted after period: {result!r}")

    # ------------------------------------------------------------------ #
    # test_es_multiple_no_space_sentences                                  #
    # ------------------------------------------------------------------ #
    def test_es_multiple_no_space_sentences(self):
        """'esta bien.como te llamas?bien gracias.' — only question sentence marked."""
        result = self.fixer.fix("esta bien.como te llamas?bien gracias.", language="es")
        self.assertIn("¿", result, f"¿ must appear: {result!r}")
        self.assertEqual(result.count("¿"), 1,
                         f"Exactly one ¿ expected: {result!r}")
        self.assertNotIn("¡", result, f"No ¡ expected: {result!r}")

    # ------------------------------------------------------------------ #
    # Regression: W1258 tests still pass — single sentence                #
    # ------------------------------------------------------------------ #
    def test_es_single_question_still_works(self):
        """Single 'como te llamas?' → '¿Como te llamas?'."""
        result = self.fixer.fix("como te llamas?", language="es")
        self.assertTrue(result.lstrip().startswith("¿"),
                        f"Single question must start with ¿: {result!r}")
        self.assertNotIn("¿¿", result)

    def test_es_existing_markers_not_duplicated_after_no_space_fix(self):
        """'¿dime.como te llamas?' — existing ¿ at start not duplicated."""
        # Note: the ¿ is at the start, so the whole thing is treated as one
        # already-marked text. The per-sentence logic should not double ¿.
        result = self.fixer.fix("¿dime.como te llamas?", language="es")
        self.assertNotIn("¿¿", result,
                         f"Double ¿¿ must not appear: {result!r}")


if __name__ == "__main__":
    unittest.main()
