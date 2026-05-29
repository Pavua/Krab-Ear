"""W1591: backward-compat alias ParakeetAdapter -> ParakeetSTTAdapter.

Regression test for the W1525 scanner finding: some imports used the shorter
name `ParakeetAdapter` but the class is actually `ParakeetSTTAdapter`. An alias
was added at the bottom of stt_parakeet.py to resolve the discrepancy without
renaming the canonical class.
"""
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestParakeetAdapterAlias(unittest.TestCase):
    """Verify the backward-compat alias is importable and identical."""

    def test_parakeet_adapter_alias_exists(self):
        """ParakeetAdapter is importable from core.pipeline.stt_parakeet."""
        from core.pipeline.stt_parakeet import ParakeetAdapter, ParakeetSTTAdapter
        self.assertIs(
            ParakeetAdapter,
            ParakeetSTTAdapter,
            "ParakeetAdapter must be the exact same object as ParakeetSTTAdapter",
        )

    def test_parakeet_adapter_alias_is_class(self):
        """ParakeetAdapter must be a class, not a module or function."""
        from core.pipeline.stt_parakeet import ParakeetAdapter
        self.assertTrue(isinstance(ParakeetAdapter, type))

    def test_parakeet_adapter_alias_instantiable(self):
        """ParakeetAdapter can be instantiated (adapter skips gracefully without lib)."""
        from core.pipeline.stt_parakeet import ParakeetAdapter
        adapter = ParakeetAdapter()
        # is_available() returns False when parakeet_mlx is not installed (CI env)
        # Just verify the attribute exists; the value depends on optional dep.
        self.assertIsInstance(adapter.is_available(), bool)


if __name__ == "__main__":
    unittest.main()
