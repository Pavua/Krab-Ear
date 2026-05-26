"""Tests for W1277 F4 MED: topic_tracker TF-IDF doc_freq set-based scan speedup.

Verifies:
1. test_doc_freq_set_speedup_correctness — set-based doc_freq gives same result as list-based.
2. test_tfidf_unchanged_after_set_optimization — full _compute_tfidf output is identical
   before/after optimization (AST-verified: the set path is active in the module).
"""

from __future__ import annotations

import ast
import sys
import unittest
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import math

from core.topic_tracker import _compute_tfidf, _tokenize  # noqa: E402


def _compute_tfidf_list_reference(
    window_tokens,
    all_windows_tokens,
):
    """Reference implementation using naive list `in` scan (pre-optimization)."""
    if not window_tokens:
        return {}

    total_windows = len(all_windows_tokens) or 1
    tf = Counter(window_tokens)
    total_tf = len(window_tokens)

    scores = {}
    for word, count in tf.items():
        tf_val = count / total_tf
        doc_freq = sum(1 for w_tokens in all_windows_tokens if word in w_tokens)
        idf_val = math.log((total_windows + 1) / (doc_freq + 1)) + 1.0
        scores[word] = tf_val * idf_val

    return scores


class TestDocFreqSetSpeedupCorrectness(unittest.TestCase):
    """Verifies set-based doc_freq produces identical results to list-based reference."""

    def _assert_scores_equal(self, window_tokens, all_windows_tokens):
        expected = _compute_tfidf_list_reference(window_tokens, all_windows_tokens)
        actual = _compute_tfidf(window_tokens, all_windows_tokens)
        self.assertEqual(
            set(expected.keys()),
            set(actual.keys()),
            "Score keys differ",
        )
        for word in expected:
            self.assertAlmostEqual(
                expected[word],
                actual[word],
                places=10,
                msg=f"Score differs for word={word!r}",
            )

    def test_doc_freq_set_speedup_correctness_small(self):
        """Small window set: 3 windows, 5 terms each."""
        windows = [
            ["hello", "world", "foo", "bar", "baz"],
            ["hello", "python", "foo", "qux", "quux"],
            ["world", "python", "corge", "grault", "garply"],
        ]
        self._assert_scores_equal(windows[0], windows)

    def test_doc_freq_set_speedup_correctness_single_window(self):
        """Single window: doc_freq always equals 1 for every term."""
        windows = [["apple", "banana", "cherry"]]
        self._assert_scores_equal(windows[0], windows)

    def test_doc_freq_set_speedup_correctness_term_in_all_windows(self):
        """Term present in every window → doc_freq == total_windows → IDF approaches 1."""
        common = "shared"
        windows = [
            [common, "a", "b"],
            [common, "c", "d"],
            [common, "e", "f"],
            [common, "g", "h"],
        ]
        self._assert_scores_equal(windows[0], windows)

    def test_doc_freq_set_speedup_correctness_term_in_no_other_window(self):
        """Term present only in current window → doc_freq == 1 → max IDF."""
        windows = [
            ["unique_word", "common"],
            ["common", "other"],
            ["another", "common"],
        ]
        self._assert_scores_equal(windows[0], windows)

    def test_doc_freq_set_speedup_correctness_larger_n500(self):
        """n=500 windows (the measured 3.1s case) — correctness, not timing."""
        import random
        rng = random.Random(42)
        vocab = [f"word{i}" for i in range(80)]
        windows = [rng.sample(vocab, k=10) for _ in range(500)]
        current = windows[0]
        self._assert_scores_equal(current, windows)

    def test_doc_freq_set_speedup_correctness_empty_window(self):
        """Empty current window returns empty dict."""
        windows = [["hello", "world"], ["foo", "bar"]]
        result = _compute_tfidf([], windows)
        self.assertEqual(result, {})

    def test_doc_freq_set_speedup_correctness_duplicate_tokens(self):
        """Duplicate tokens in a window: TF weighting correct, doc_freq unaffected."""
        windows = [
            ["hello", "hello", "world"],
            ["hello", "foo"],
            ["world", "bar"],
        ]
        self._assert_scores_equal(windows[0], windows)


class TestTfidfUnchangedAfterSetOptimization(unittest.TestCase):
    """Verifies the optimized code path is active (AST check) and outputs match."""

    def test_tfidf_unchanged_after_set_optimization(self):
        """Full _compute_tfidf scores are identical between list-ref and optimized impl."""
        windows = [
            _tokenize("the quick brown fox jumps over the lazy dog"),
            _tokenize("pack my box with five dozen liquor jugs"),
            _tokenize("how vexingly quick daft zebras jump"),
        ]
        for i, current in enumerate(windows):
            expected = _compute_tfidf_list_reference(current, windows)
            actual = _compute_tfidf(current, windows)
            for word in expected:
                self.assertAlmostEqual(
                    expected[word],
                    actual[word],
                    places=10,
                    msg=f"window={i}, word={word!r}: score mismatch",
                )

    def test_set_conversion_present_in_source_ast(self):
        """AST check: _compute_tfidf source contains a set comprehension over all_windows_tokens."""
        source_path = (
            Path(__file__).resolve().parents[1] / "core" / "topic_tracker.py"
        )
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find _compute_tfidf function definition
        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_compute_tfidf":
                func_node = node
                break

        self.assertIsNotNone(func_node, "_compute_tfidf function not found in AST")

        # Look for a set comprehension or ListComp that calls set() over all_windows_tokens
        found_set_conversion = False
        for node in ast.walk(func_node):
            # Pattern: [set(toks) for toks in all_windows_tokens]
            if isinstance(node, ast.ListComp):
                elt = node.elt
                if (
                    isinstance(elt, ast.Call)
                    and isinstance(elt.func, ast.Name)
                    and elt.func.id == "set"
                ):
                    found_set_conversion = True
                    break

        self.assertTrue(
            found_set_conversion,
            "Expected a [set(...) for ... in all_windows_tokens] list comprehension "
            "inside _compute_tfidf — set-based optimization not present in AST",
        )

    def test_membership_check_uses_set_variable(self):
        """AST check: inner generator/comprehension uses tok_set (not w_tokens) for membership."""
        source_path = (
            Path(__file__).resolve().parents[1] / "core" / "topic_tracker.py"
        )
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_compute_tfidf":
                func_node = node
                break

        self.assertIsNotNone(func_node)

        # Look for: sum(1 for tok_set in window_token_sets if word in tok_set)
        # Specifically: a GeneratorExp whose iter iterates a Name ending with 'sets'
        # and whose if-test is a Compare with word in tok_set
        found_set_iter = False
        for node in ast.walk(func_node):
            if isinstance(node, ast.GeneratorExp):
                for gen in node.generators:
                    if (
                        isinstance(gen.iter, ast.Name)
                        and "set" in gen.iter.id
                        and gen.ifs
                    ):
                        # Check the if-condition uses `in` with the loop variable
                        cond = gen.ifs[0]
                        if (
                            isinstance(cond, ast.Compare)
                            and any(isinstance(op, ast.In) for op in cond.ops)
                            and isinstance(cond.comparators[0], ast.Name)
                            and cond.comparators[0].id == gen.target.id  # type: ignore[attr-defined]
                        ):
                            found_set_iter = True
                            break

        self.assertTrue(
            found_set_iter,
            "Expected generator 'sum(1 for tok_set in window_token_sets if word in tok_set)' "
            "not found — optimization may not have been applied correctly",
        )


if __name__ == "__main__":
    unittest.main()
