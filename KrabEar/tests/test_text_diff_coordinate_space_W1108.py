"""Tests for coordinate_space disambiguation in DiffChange (W1097 F1 MED).

Verifies that DiffChange.coordinate_space correctly identifies which word
array the `position` field indexes into: "orig" for orig_words,
"new" for new_words.
"""

from __future__ import annotations

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.text_diff import TextDiffAnalyzer, DiffChange  # noqa: E402


class TestCoordinateSpaceAdded(unittest.TestCase):
    """'added' changes must use coordinate_space='new'."""

    def test_added_change_has_new_coordinate_space(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello", "hello world")
        added = [c for c in result.changes if c.type == "added"]
        self.assertTrue(added, "Expected at least one 'added' change")
        for change in added:
            self.assertEqual(
                change.coordinate_space,
                "new",
                f"'added' change '{change.text}' at position {change.position} "
                f"should have coordinate_space='new', got '{change.coordinate_space}'",
            )


class TestCoordinateSpaceRemoved(unittest.TestCase):
    """'removed' changes must use coordinate_space='orig'."""

    def test_removed_change_has_orig_coordinate_space(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello world", "hello")
        removed = [c for c in result.changes if c.type == "removed"]
        self.assertTrue(removed, "Expected at least one 'removed' change")
        for change in removed:
            self.assertEqual(
                change.coordinate_space,
                "orig",
                f"'removed' change '{change.text}' at position {change.position} "
                f"should have coordinate_space='orig', got '{change.coordinate_space}'",
            )


class TestCoordinateSpaceUnchanged(unittest.TestCase):
    """'unchanged' changes must use coordinate_space='orig'."""

    def test_unchanged_change_has_orig_coordinate_space(self):
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("hello world", "hello world")
        unchanged = [c for c in result.changes if c.type == "unchanged"]
        self.assertTrue(unchanged, "Expected at least one 'unchanged' change")
        for change in unchanged:
            self.assertEqual(
                change.coordinate_space,
                "orig",
                f"'unchanged' change '{change.text}' at position {change.position} "
                f"should have coordinate_space='orig', got '{change.coordinate_space}'",
            )


class TestCoordinateSpaceDisambiguates(unittest.TestCase):
    """Two changes with position=0 must be distinguishable via coordinate_space."""

    def test_two_position_zero_entries_disambiguated(self):
        """orig='foo bar', new='baz bar' → removed 'foo' at orig[0], added 'baz' at new[0].

        Without coordinate_space, both have position=0, which is ambiguous.
        With coordinate_space, 'removed' → "orig", 'added' → "new".
        """
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("foo bar", "baz bar")

        removed = [c for c in result.changes if c.type == "removed"]
        added = [c for c in result.changes if c.type == "added"]

        self.assertTrue(removed, "Expected at least one 'removed' change")
        self.assertTrue(added, "Expected at least one 'added' change")

        # Verify that there is at least one pair with the same position value
        # (demonstrating the ambiguity that coordinate_space resolves).
        removed_positions = {c.position for c in removed}
        added_positions = {c.position for c in added}
        self.assertTrue(
            removed_positions & added_positions,
            "Expected overlapping position values between 'removed' and 'added' changes; "
            "this is the ambiguity that coordinate_space resolves.",
        )

        # Now verify disambiguation via coordinate_space.
        for change in removed:
            self.assertEqual(change.coordinate_space, "orig")
        for change in added:
            self.assertEqual(change.coordinate_space, "new")

    def test_insert_only_added_uses_new_space(self):
        """Pure insert (orig empty): all changes are 'added' with coordinate_space='new'."""
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("", "alpha beta")
        added = [c for c in result.changes if c.type == "added"]
        self.assertTrue(added)
        for change in added:
            self.assertEqual(change.coordinate_space, "new")

    def test_delete_only_removed_uses_orig_space(self):
        """Pure delete (new empty): all changes are 'removed' with coordinate_space='orig'."""
        analyzer = TextDiffAnalyzer()
        result = analyzer.compute_diff("alpha beta", "")
        removed = [c for c in result.changes if c.type == "removed"]
        self.assertTrue(removed)
        for change in removed:
            self.assertEqual(change.coordinate_space, "orig")


class TestDiffChangeDefaultCoordinateSpace(unittest.TestCase):
    """DiffChange default coordinate_space is 'orig' for backward compat."""

    def test_default_is_orig(self):
        c = DiffChange(type="unchanged", text="word", position=0)
        self.assertEqual(c.coordinate_space, "orig")

    def test_explicit_new_space(self):
        c = DiffChange(type="added", text="word", position=5, coordinate_space="new")
        self.assertEqual(c.coordinate_space, "new")
        self.assertEqual(c.position, 5)

    def test_position_field_still_present(self):
        """Backward compat: existing callers reading .position still work."""
        c = DiffChange(type="removed", text="old", position=3)
        self.assertEqual(c.position, 3)


if __name__ == "__main__":
    unittest.main()
