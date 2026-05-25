"""Tests for KrabEar/core/text_diff.py — TextDiffAnalyzer."""

from core.text_diff import TextDiffAnalyzer, TextDiffResult, DiffChange
import sys
import os
import unittest

# Ensure KrabEar package root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


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

    def test_completely_different_only_add_del(self):
        """All words in the diff are added or removed — no unchanged ops."""
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("alpha beta gamma", "one two three")
        unchanged = [c for c in result.changes if c.type == "unchanged"]
        self.assertEqual(unchanged, [])
        removed = [c for c in result.changes if c.type == "removed"]
        added = [c for c in result.changes if c.type == "added"]
        self.assertEqual(result.words_removed, 3)
        self.assertEqual(result.words_added, 3)
        self.assertEqual(len(removed), 3)
        self.assertEqual(len(added), 3)

    def test_identical_all_equal_ops(self):
        """All change ops are 'unchanged' for identical texts."""
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello world foo", "hello world foo")
        types = [c.type for c in result.changes]
        self.assertTrue(all(t == "unchanged" for t in types))
        self.assertEqual(len(types), 3)

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


class TestTextDiffAnalyzerPartialEdit(unittest.TestCase):
    """Partial edit: mix of equal + changed ops."""

    def test_partial_edit_has_equal_and_changed(self):
        """Single word swap keeps surrounding words as unchanged."""
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("the quick brown fox", "the slow brown fox")
        unchanged = [c for c in result.changes if c.type == "unchanged"]
        removed = [c for c in result.changes if c.type == "removed"]
        added = [c for c in result.changes if c.type == "added"]
        # "the", "brown", "fox" stay unchanged
        self.assertGreaterEqual(len(unchanged), 3)
        self.assertEqual(result.words_removed, 1)
        self.assertEqual(result.words_added, 1)
        self.assertTrue(any(c.text == "quick" for c in removed))
        self.assertTrue(any(c.text == "slow" for c in added))

    def test_partial_edit_similarity_between_zero_and_one(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("the quick brown fox", "the slow brown fox")
        self.assertGreater(result.similarity_ratio, 0.0)
        self.assertLess(result.similarity_ratio, 1.0)


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


class TestTextDiffWave131(unittest.TestCase):
    """Wave 131 required test cases for TextDiffAnalyzer."""

    def setUp(self):
        self.analyzer = TextDiffAnalyzer()

    # ------------------------------------------------------------------
    # test_identical_returns_empty_diff
    # ------------------------------------------------------------------
    def test_identical_returns_empty_diff(self):
        result = self.analyzer.compute_diff("hello world", "hello world")
        added = [c for c in result.changes if c.type != "unchanged"]
        self.assertEqual(added, [])
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    # ------------------------------------------------------------------
    # test_word_inserted
    # ------------------------------------------------------------------
    def test_word_inserted(self):
        result = self.analyzer.compute_diff("foo bar", "foo baz bar")
        added = [c for c in result.changes if c.type == "added"]
        self.assertEqual(result.words_added, 1)
        self.assertTrue(any(c.text == "baz" for c in added))

    # ------------------------------------------------------------------
    # test_word_deleted
    # ------------------------------------------------------------------
    def test_word_deleted(self):
        result = self.analyzer.compute_diff("alpha beta gamma", "alpha gamma")
        removed = [c for c in result.changes if c.type == "removed"]
        self.assertEqual(result.words_removed, 1)
        self.assertTrue(any(c.text == "beta" for c in removed))

    # ------------------------------------------------------------------
    # test_word_replaced
    # ------------------------------------------------------------------
    def test_word_replaced(self):
        result = self.analyzer.compute_diff("the old word here", "the new word here")
        removed = [c for c in result.changes if c.type == "removed"]
        added = [c for c in result.changes if c.type == "added"]
        self.assertEqual(result.words_removed, 1)
        self.assertEqual(result.words_added, 1)
        self.assertTrue(any(c.text == "old" for c in removed))
        self.assertTrue(any(c.text == "new" for c in added))

    # ------------------------------------------------------------------
    # test_unicode_words
    # ------------------------------------------------------------------
    def test_unicode_words(self):
        result = self.analyzer.compute_diff("привет мир дела", "привет свет дела")
        removed = [c for c in result.changes if c.type == "removed"]
        added = [c for c in result.changes if c.type == "added"]
        unchanged = [c for c in result.changes if c.type == "unchanged"]
        self.assertTrue(any(c.text == "мир" for c in removed))
        self.assertTrue(any(c.text == "свет" for c in added))
        # "привет" and "дела" should be unchanged
        self.assertTrue(any(c.text == "привет" for c in unchanged))
        self.assertTrue(any(c.text == "дела" for c in unchanged))

    def test_unicode_similarity_range(self):
        result = self.analyzer.compute_diff("こんにちは 世界", "こんにちは 友達")
        self.assertGreaterEqual(result.similarity_ratio, 0.0)
        self.assertLessEqual(result.similarity_ratio, 1.0)

    # ------------------------------------------------------------------
    # test_handles_whitespace_changes
    # ------------------------------------------------------------------
    def test_handles_whitespace_changes(self):
        """Extra whitespace collapses at word level — no crash, sensible results."""
        result = self.analyzer.compute_diff("hello   world", "hello world")
        # Both split to same two words → should report no meaningful diff
        self.assertIsInstance(result, TextDiffResult)
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    def test_handles_leading_trailing_whitespace(self):
        result = self.analyzer.compute_diff("  hello world  ", "hello world")
        self.assertIsInstance(result, TextDiffResult)
        # str.split() strips leading/trailing, so word lists are identical
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    def test_handles_newlines_as_whitespace(self):
        """Newlines are treated as whitespace in word splitting."""
        result = self.analyzer.compute_diff("line one\nline two", "line one line two")
        self.assertIsInstance(result, TextDiffResult)
        self.assertEqual(result.words_added, 0)
        self.assertEqual(result.words_removed, 0)

    # ------------------------------------------------------------------
    # test_concurrent_diff
    # ------------------------------------------------------------------
    def test_concurrent_diff(self):
        """Concurrent calls on the same analyzer instance are safe."""
        import threading
        results = {}
        errors = []

        def worker(idx: int) -> None:
            try:
                orig = f"word{idx} stays removed{idx}"
                rewr = f"word{idx} stays added{idx}"
                res = self.analyzer.compute_diff(orig, rewr)
                results[idx] = res
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        for idx, res in results.items():
            removed = [c for c in res.changes if c.type == "removed"]
            added = [c for c in res.changes if c.type == "added"]
            self.assertEqual(res.words_removed, 1, f"thread {idx}")
            self.assertEqual(res.words_added, 1, f"thread {idx}")
            self.assertTrue(any(c.text == f"removed{idx}" for c in removed), f"thread {idx}")
            self.assertTrue(any(c.text == f"added{idx}" for c in added), f"thread {idx}")


if __name__ == "__main__":
    unittest.main()
