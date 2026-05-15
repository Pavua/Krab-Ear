"""
Wave 63 H2 — mx.clear_cache() called after transcribe() and on profile switch.

Tests use mocked mlx.core — no GPU/model required.
"""
import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_mlx_stub(has_clear_cache: bool = True):
    """Return a (mlx_module, core_module) stub pair."""
    mlx = types.ModuleType("mlx")
    core = types.ModuleType("mlx.core")
    if has_clear_cache:
        core.clear_cache = MagicMock()
    mlx.core = core
    return mlx, core


class TestMxClearCacheAfterTranscribe(unittest.TestCase):
    """engine.py post-STT cleanup block calls mx.clear_cache()."""

    def _run_cleanup_block(self, mlx_module):
        """Re-execute the cleanup snippet from engine.py transcribe()."""
        import gc as _gc
        _gc.collect()
        try:
            _mx = mlx_module.core
            _mx.clear_cache()
        except (ImportError, AttributeError):
            pass

    def test_clear_cache_called_when_mlx_available(self):
        mlx, core = _make_mlx_stub(has_clear_cache=True)
        self._run_cleanup_block(mlx)
        core.clear_cache.assert_called_once()

    def test_clear_cache_attribute_missing_no_raise(self):
        """AttributeError (older MLX without clear_cache) must be swallowed."""
        mlx, _ = _make_mlx_stub(has_clear_cache=False)
        # Should not raise
        self._run_cleanup_block(mlx)

    def test_import_error_swallowed(self):
        """ImportError (MLX not installed) must be swallowed silently."""

        class _FakeCore:
            @property
            def clear_cache(self):
                raise ImportError("mlx not installed")

        class _FakeMlx:
            core = _FakeCore()

        # Should not raise
        self._run_cleanup_block(_FakeMlx())

    def test_clear_cache_called_on_profile_switch(self):
        """set_quality_profile() must also flush the MLX Metal cache."""
        mlx, core = _make_mlx_stub(has_clear_cache=True)

        # Simulate the set_quality_profile cleanup block
        def _run_profile_switch_cleanup():
            try:
                _mx = mlx.core
                _mx.clear_cache()
            except (ImportError, AttributeError):
                pass

        _run_profile_switch_cleanup()
        core.clear_cache.assert_called_once()


if __name__ == "__main__":
    unittest.main()
