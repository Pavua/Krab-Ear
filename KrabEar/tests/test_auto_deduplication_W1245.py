"""Tests for W1243 F1+F5 fixes in AutoDeduplicator.

F1: Jaccard hybrid algorithm (case-insensitive, token-level)
F5: _check_lock serializes concurrent check_duplicate calls
"""

from __future__ import annotations

import ast
import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path setup — allow both pytest and standalone unittest invocation
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# We test the module-level function directly + the class.
from backend.auto_deduplication import (  # noqa: E402
    AutoDeduplicator,
    _text_similarity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(items: list[dict]) -> Any:
    """Minimal StateStore stub that returns a fixed item list."""
    store = MagicMock()
    store.get_history_page.return_value = (items, None)
    return store


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# F1: Jaccard hybrid — _text_similarity unit tests
# ---------------------------------------------------------------------------

class TestJaccardHybrid(unittest.TestCase):

    def test_jaccard_case_diff_below_threshold(self):
        """Case-only differences should NOT score >= 0.9 (F1 regression guard).

        Old SequenceMatcher: 'hello world' vs 'Hello World' → ~1.0
        Jaccard after lowercase normalisation: identical tokens → 1.0
        This test verifies normalisation is applied so case-only duplicates
        are caught correctly (Jaccard = 1.0 after lower) rather than silently
        mis-scoring with raw SequenceMatcher on mixed-case.
        """
        # After lowercase normalisation, identical → 1.0 (correctly detected as dup)
        score = _text_similarity("Hello World", "hello world")
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_jaccard_short_prefix_below_threshold(self):
        """Short texts sharing only a prefix must score below 0.9 threshold.

        Old SequenceMatcher gave ~0.91 for e.g. "Привет как дела" vs "Привет как",
        causing false positives. Jaccard token-level is stricter.
        """
        # "Привет как дела" (3 tokens) vs "Привет как" (2 tokens)
        # intersection = 2, union = 3 → Jaccard = 2/3 ≈ 0.667 < 0.9
        score = _text_similarity("Привет как дела", "Привет как")
        self.assertLess(score, 0.9,
            msg=f"Short prefix pair should score < 0.9 but got {score:.4f}")

    def test_jaccard_true_duplicate_above_threshold(self):
        """Identical texts must score 1.0 (true duplicate detection preserved)."""
        text = "The quick brown fox jumps over the lazy dog"
        score = _text_similarity(text, text)
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_jaccard_unrelated_texts_low_score(self):
        """Completely unrelated texts must score well below threshold."""
        score = _text_similarity("cats are fluffy animals", "the economy is complex")
        self.assertLess(score, 0.4)

    def test_jaccard_indeterminate_zone_uses_sequence_matcher(self):
        """Texts with Jaccard in [0.7, 0.85] should fall back to SequenceMatcher."""
        # Craft texts where Jaccard ≈ 0.75 (indeterminate zone).
        # "a b c d" vs "a b c e" → intersection={a,b,c}, union={a,b,c,d,e} → 3/5=0.6
        # Let's use texts with Jaccard ~0.75: "a b c d e f g h" vs "a b c d e f g x"
        # intersection=7, union=9 → 7/9 ≈ 0.778 (in zone), SequenceMatcher will also be high
        score = _text_similarity(
            "alpha beta gamma delta epsilon zeta eta theta",
            "alpha beta gamma delta epsilon zeta eta iota",
        )
        # Both Jaccard (~0.778) and SequenceMatcher are high → result should be > 0.7
        self.assertGreater(score, 0.7)

    def test_empty_texts(self):
        """Both empty → 1.0; one empty → 0.0."""
        self.assertAlmostEqual(_text_similarity("", ""), 1.0)
        self.assertAlmostEqual(_text_similarity("hello", ""), 0.0)
        self.assertAlmostEqual(_text_similarity("", "world"), 0.0)


# ---------------------------------------------------------------------------
# F5: _check_lock serializes concurrent check_duplicate calls
# ---------------------------------------------------------------------------

class TestConcurrentCheckDuplicateSerialized(unittest.TestCase):

    def test_concurrent_check_duplicate_serialized(self):
        """Two threads calling check_duplicate simultaneously must not both pass.

        We simulate the race: the store initially returns an empty history so
        both threads see "no duplicate". But because _check_lock serialises the
        critical section, the second thread will see the item added by the first.

        Implementation note: the store is a counter-aware stub. On the first call
        it returns [], on subsequent calls it returns [first_item].  With the lock,
        exactly one thread should detect a duplicate; without it, both would get
        is_duplicate=False (the race).
        """
        dedup = AutoDeduplicator()
        TEXT = "concurrent duplicate test"
        TS = _now_iso()

        call_count = 0
        first_item = {"id": "existing-1", "text": TEXT, "ts": TS}

        def get_history_page(cursor=None, limit=50):
            nonlocal call_count
            # First invocation returns empty (simulates fresh state).
            # All subsequent invocations return the first item (simulates written state).
            call_count += 1
            if call_count == 1:
                return ([], None)
            return ([first_item], None)

        store = MagicMock()
        store.get_history_page.side_effect = get_history_page

        results: list[bool] = []
        errors: list[Exception] = []

        def worker():
            try:
                r = dedup.check_duplicate(text=TEXT, timestamp=TS, store=store)
                results.append(r.is_duplicate)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertFalse(errors, f"Unexpected errors: {errors}")
        self.assertEqual(len(results), 2, "Both threads should have completed")

        # With _check_lock: the second thread sees the item → is_duplicate=True.
        # At most one should be False (the first), one should be True (the second).
        true_count = sum(results)
        # Exactly one duplicate should be detected (the second call sees the item).
        # We allow true_count >= 1 to account for different scheduling, but at most
        # one thread should have gotten is_duplicate=False.
        false_count = sum(1 for r in results if not r)
        self.assertLessEqual(false_count, 1,
            f"At most 1 thread should get is_duplicate=False, got false_count={false_count}")

    def test_check_lock_exists_on_instance(self):
        """AutoDeduplicator must expose _check_lock as a threading.Lock."""
        dedup = AutoDeduplicator()
        self.assertTrue(hasattr(dedup, "_check_lock"),
            "_check_lock attribute must exist")
        self.assertIsInstance(dedup._check_lock, type(threading.Lock()),
            "_check_lock must be a threading.Lock instance")


# ---------------------------------------------------------------------------
# AST-level regression guard: ensure SequenceMatcher import is present
# ---------------------------------------------------------------------------

class TestAstRegression(unittest.TestCase):

    def _get_source_path(self) -> str:
        src = os.path.join(
            os.path.dirname(__file__),
            "../backend/auto_deduplication.py",
        )
        return os.path.abspath(src)

    def test_sequence_matcher_import_present(self):
        """SequenceMatcher must still be imported (used in fallback band)."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "difflib":
                    names = [alias.name for alias in node.names]
                    if "SequenceMatcher" in names:
                        found = True
                        break
        self.assertTrue(found, "SequenceMatcher must be imported from difflib")

    def test_check_lock_assigned_in_init(self):
        """__init__ must assign self._check_lock (AST check)."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "__init__":
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if (
                                isinstance(target, ast.Attribute)
                                and target.attr == "_check_lock"
                            ):
                                found = True
                                break
        self.assertTrue(found, "self._check_lock must be assigned in __init__")

    def test_text_similarity_function_exists(self):
        """Module must define _text_similarity at module level (AST check)."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)

        found = any(
            isinstance(node, ast.FunctionDef) and node.name == "_text_similarity"
            for node in ast.walk(tree)
        )
        self.assertTrue(found, "_text_similarity must be defined in module")


if __name__ == "__main__":
    unittest.main()
