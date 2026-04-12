"""Tests for KrabEar/core/text_diff.py — TextDiffAnalyzer."""

import sys
import os
import unittest

# Ensure KrabEar package root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.text_diff import TextDiffAnalyzer, TextDiffResult, DiffChange


class TestTextDiffAnalyzerIdentical(unittest.TestCase):
    """Identical texts → no changes."""

    def test_identical_texts_no_changes(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello world", "hello world")
        added = [c for c in result.changes if c.type == "added"]
        removed = [c for c in result.changes if c.type == "removed"]
        self.assertEqual(added, [])
        self.assertEqual(removed, [])
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    def test_identical_texts_similarity_one(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("test sentence", "test sentence")
        self.assertAlmostEqual(result.similarity_ratio, 1.0, places=3)

    def test_identical_texts_summary_no_changes(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("abc def", "abc def")
        self.assertIn("no changes", result.summary)


class TestTextDiffAnalyzerEmpty(unittest.TestCase):
    """Edge cases with empty strings."""

    def test_both_empty(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("", "")
        self.assertIsInstance(result, TextDiffResult)
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    def test_original_empty_rewritten_not(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("", "new word")
        self.assertEqual(result.words_added, 2)
        self.assertEqual(result.words_removed, 0)

    def test_rewritten_empty_original_not(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("old text", "")
        self.assertEqual(result.words_removed, 2)
        self.assertEqual(result.words_added, 0)


class TestTextDiffAnalyzerChanges(unittest.TestCase):
    """Tests for detecting actual word-level changes."""

    def test_one_word_replaced(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("the cat sat", "the dog sat")
        removed = [c for c in result.changes if c.type == "removed"]
        added = [c for c in result.changes if c.type == "added"]
        self.assertTrue(any(c.text == "cat" for c in removed))
        self.assertTrue(any(c.text == "dog" for c in added))
        self.assertEqual(result.words_removed, 1)
        self.assertEqual(result.words_added, 1)

    def test_word_added_at_end(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello", "hello world")
        self.assertEqual(result.words_added, 1)
        self.assertEqual(result.words_removed, 0)
        added = [c for c in result.changes if c.type == "added"]
        self.assertEqual(added[0].text, "world")

    def test_word_removed(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("very good result", "good result")
        self.assertEqual(result.words_removed, 1)
        removed = [c for c in result.changes if c.type == "removed"]
        self.assertTrue(any(c.text == "very" for c in removed))

    def test_unchanged_words_counted(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("alpha beta gamma", "alpha BETA gamma")
        # "alpha" and "gamma" are unchanged
        self.assertGreaterEqual(result.words_unchanged, 2)


class TestTextDiffAnalyzerSimilarity(unittest.TestCase):
    """Similarity ratio is between 0 and 1."""

    def test_completely_different_texts(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("abc", "xyz")
        self.assertGreaterEqual(result.similarity_ratio, 0.0)
        self.assertLessEqual(result.similarity_ratio, 1.0)

    def test_similarity_decreases_with_more_changes(self):
        analyzer = TextDiffAnalyzer()
        r1 = analyzer.compute_diff("hello world foo bar", "hello world foo baz")
        r2 = analyzer.compute_diff("hello world foo bar", "completely different text here")
        self.assertGreater(r1.similarity_ratio, r2.similarity_ratio)


class TestTextDiffAnalyzerSummary(unittest.TestCase):
    """Summary string contains meaningful info."""

    def test_summary_mentions_added_words(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello", "hello world today")
        self.assertIn("added", result.summary)
        self.assertIn("2", result.summary)

    def test_summary_mentions_removed_words(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("alpha beta gamma", "alpha gamma")
        self.assertIn("removed", result.summary)

    def test_summary_mentions_similarity(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello world", "hello world")
        self.assertIn("%", result.summary)

    def test_summary_llm_prefix(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("text before", "text after change")
        self.assertTrue(result.summary.startswith("LLM") or "no changes" in result.summary)


class TestDiffChangeDataclass(unittest.TestCase):
    """DiffChange has correct fields."""

    def test_diff_change_fields(self):
        c = DiffChange(type="added", text="hello", position=3)
        self.assertEqual(c.type, "added")
        self.assertEqual(c.text, "hello")
        self.assertEqual(c.position, 3)

    def test_diff_result_is_dataclass(self):
        r = TextDiffResult()
        self.assertEqual(r.changes, [])
        self.assertEqual(r.words_added, 0)
        self.assertEqual(r.similarity_ratio, 0.0)


class TestTextDiffAnalyzerRussian(unittest.TestCase):
    """Russian-text specific scenarios (primary use case for LLM rewriter)."""

    def test_punctuation_only_change(self):
        """Adding punctuation changes chars but not words."""
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff(
            "привет мир как дела",
            "привет мир, как дела."
        )
        # Word count should be same or nearly same
        self.assertEqual(result.words_unchanged + result.words_added + result.words_removed, 4 + result.words_added)

    def test_multi_sentence_rewrite(self):
        analyzer = TextDiffAnalyzer()
        original = "это текст без знаков препинания много слов подряд"
        rewritten = "Это текст без знаков препинания, много слов подряд."
        result = analyzer.compute_diff(original, rewritten)
        self.assertIsInstance(result, TextDiffResult)
        self.assertGreater(result.similarity_ratio, 0.5)


if __name__ == "__main__":
    unittest.main()
