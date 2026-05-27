"""Tests for W1097 F2 LOW fix — strip_punctuation parameter in compute_diff().

Three required cases:
1. "дела?" matches "дела" with strip_punctuation=True (new default).
2. strip_punctuation=False preserves old behaviour (treats them as different).
3. LLM rewrite scenario: only punctuation changed → 0 word change.
"""

from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.text_diff import TextDiffAnalyzer  # noqa: E402


class TestStripPunctuationDefault(unittest.TestCase):
    """strip_punctuation=True is the new default."""

    def setUp(self):
        self.analyzer = TextDiffAnalyzer()

    # ------------------------------------------------------------------
    # Case 1 — "дела?" matches "дела" with strip=True
    # ------------------------------------------------------------------
    def test_ru_word_with_question_mark_matches_bare_word(self):
        """«дела?» and «дела» must be equal under default strip=True."""
        result = self.analyzer.compute_diff("как дела?", "как дела")
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)
        unchanged = [c for c in result.changes if c.type == "unchanged"]
        # Both "как" and "дела"/"дела?" map to equal words
        self.assertEqual(len(unchanged), 2)

    def test_ru_sentence_trailing_period_no_change(self):
        """Adding a period to the last word must not count as a word change."""
        result = self.analyzer.compute_diff(
            "привет мир как дела",
            "привет мир как дела.",
        )
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    def test_es_word_with_exclamation_matches(self):
        """Spanish «hola!» equals «hola» when strip=True."""
        result = self.analyzer.compute_diff("hola amigo!", "hola amigo")
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    def test_leading_punctuation_stripped(self):
        """Leading punctuation (e.g. «"word») is stripped before comparison."""
        result = self.analyzer.compute_diff('"hello world"', "hello world")
        # «"hello» vs «hello» and «world"» vs «world» → both should match
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    # ------------------------------------------------------------------
    # Case 2 — strip_punctuation=False preserves old behaviour
    # ------------------------------------------------------------------
    def test_opt_out_preserves_old_behaviour_question_mark(self):
        """With strip_punctuation=False, «дела?» ≠ «дела» → 1 removed + 1 added."""
        result = self.analyzer.compute_diff(
            "как дела?", "как дела", strip_punctuation=False
        )
        # Without stripping, «дела?» != «дела»
        self.assertGreater(result.words_removed + result.words_added, 0)

    def test_opt_out_preserves_old_behaviour_period(self):
        """With strip_punctuation=False, trailing period changes word identity."""
        result = self.analyzer.compute_diff(
            "hello world", "hello world.", strip_punctuation=False
        )
        # «world.» != «world» → should show at least 1 change
        self.assertGreater(result.words_removed + result.words_added, 0)

    # ------------------------------------------------------------------
    # Case 3 — LLM rewrite scenario: only punctuation changed → 0 word change
    # ------------------------------------------------------------------
    def test_llm_punct_only_rewrite_shows_zero_word_change(self):
        """Primary use case: LLM adds commas/periods — no word change must be reported.

        Scenario: same words, only punctuation added (no capitalisation change).
        «препинания,» == «препинания» after strip; «подряд.» == «подряд» after strip.
        """
        original = "это текст без знаков препинания много слов подряд"
        rewritten = "это текст без знаков препинания, много слов подряд."
        result = self.analyzer.compute_diff(original, rewritten)
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    def test_llm_punct_only_rewrite_ru_summary_no_changes(self):
        """Summary must say 'no changes' for a punct-only rewrite."""
        original = "привет мир как дела"
        rewritten = "привет мир, как дела."
        result = self.analyzer.compute_diff(original, rewritten)
        self.assertIn("no changes", result.summary)

    def test_llm_rewrite_actual_word_change_still_detected(self):
        """Real word changes are still reported even with strip=True."""
        original = "привет мир как дела"
        rewritten = "привет свет как дела."
        result = self.analyzer.compute_diff(original, rewritten)
        # «мир» → «свет» is a real word change
        self.assertEqual(result.words_removed, 1)
        self.assertEqual(result.words_added, 1)
        removed = [c for c in result.changes if c.type == "removed"]
        added = [c for c in result.changes if c.type == "added"]
        self.assertTrue(any(c.text == "мир" for c in removed))
        self.assertTrue(any(c.text == "свет" for c in added))


class TestStripPunctRegex(unittest.TestCase):
    """Unit-level checks on the STRIP_PUNCT_RE constant."""

    def test_regex_strips_trailing_question(self):
        from core.text_diff import STRIP_PUNCT_RE
        self.assertEqual(STRIP_PUNCT_RE.sub("", "дела?"), "дела")

    def test_regex_strips_trailing_period(self):
        from core.text_diff import STRIP_PUNCT_RE
        self.assertEqual(STRIP_PUNCT_RE.sub("", "мир."), "мир")

    def test_regex_strips_leading_quote(self):
        from core.text_diff import STRIP_PUNCT_RE
        self.assertEqual(STRIP_PUNCT_RE.sub("", '"hello'), "hello")

    def test_regex_keeps_word_internal_punct(self):
        """Hyphens and apostrophes inside a word must be preserved."""
        from core.text_diff import STRIP_PUNCT_RE
        # «bien-aimé» — internal hyphen stays
        self.assertEqual(STRIP_PUNCT_RE.sub("", "bien-aimé"), "bien-aimé")

    def test_regex_unicode_cyrillic(self):
        from core.text_diff import STRIP_PUNCT_RE
        self.assertEqual(STRIP_PUNCT_RE.sub("", "«Краб»"), "Краб")

    def test_regex_empty_string(self):
        from core.text_diff import STRIP_PUNCT_RE
        self.assertEqual(STRIP_PUNCT_RE.sub("", ""), "")

    def test_regex_only_punctuation(self):
        from core.text_diff import STRIP_PUNCT_RE
        # A token that is only punctuation strips to empty string
        self.assertEqual(STRIP_PUNCT_RE.sub("", "..."), "")


if __name__ == "__main__":
    unittest.main()
