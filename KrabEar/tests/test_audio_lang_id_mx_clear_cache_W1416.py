"""W1416 — audio_lang_id.clear_model_cache() calls mx.clear_cache() (W1405 F2 MED).

Tests:
1. test_clear_model_cache_calls_mx_clear_cache_when_mlx_available
2. test_clear_model_cache_safe_when_mlx_unavailable
3. test_clear_model_cache_safe_when_mx_clear_cache_raises
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID  # noqa: E402


class TestClearModelCacheCallsMxClearCache(unittest.TestCase):
    """clear_model_cache() calls mx.clear_cache() when MLX is available."""

    def setUp(self):
        # Seed the cache with a dummy entry so there is something to clear.
        AudioLanguageID._model_cache["some-model"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_calls_mx_clear_cache_when_mlx_available(self):
        """When _HAS_MLX is True, clear_model_cache() must call mx.clear_cache()."""
        # Patch mlx.core.clear_cache directly — mlx IS installed in this env,
        # so we spy on the actual attribute rather than replacing the module.
        try:
            import mlx.core as _real_mx
            with patch("core.audio_lang_id._HAS_MLX", True), \
                 patch.object(_real_mx, "clear_cache") as mock_clear:
                AudioLanguageID.clear_model_cache()
                # assert INSIDE context so _HAS_MLX is still True during check
                mock_clear.assert_called_once()
        except ImportError:
            # MLX not installed (e.g. Ubuntu CI) — patch BOTH "mlx" and "mlx.core"
            # so that `import mlx.core as _mx_rt` inside clear_model_cache() succeeds.
            # Patching only "mlx.core" is insufficient: Python resolves the parent
            # package "mlx" first and raises ImportError when it is absent.
            # W1748: __path__ = [] is required so Python recognises the bare
            # ModuleType as a namespace package and resolves mlx.core via
            # sys.modules rather than falling back to the filesystem loader
            # (which would try to dlopen libmlx.so again and re-raise).
            import types
            mock_mlx_pkg = types.ModuleType("mlx")
            mock_mlx_pkg.__path__ = []  # mark as namespace package
            mock_mx = MagicMock()
            with patch("core.audio_lang_id._HAS_MLX", True), \
                 patch.dict("sys.modules", {"mlx": mock_mlx_pkg, "mlx.core": mock_mx}):
                AudioLanguageID.clear_model_cache()
                # assert INSIDE context while sys.modules patch is active
                mock_mx.clear_cache.assert_called_once()

        self.assertEqual(len(AudioLanguageID._model_cache), 0,
                         "cache должен быть пуст после clear_model_cache()")

    def test_clear_model_cache_clears_python_dict(self):
        """Python dict is cleared regardless of MLX availability."""
        with patch("core.audio_lang_id._HAS_MLX", False):
            AudioLanguageID.clear_model_cache()

        self.assertEqual(len(AudioLanguageID._model_cache), 0)


class TestClearModelCacheSafeWhenMlxUnavailable(unittest.TestCase):
    """clear_model_cache() does NOT crash when MLX is not installed."""

    def setUp(self):
        AudioLanguageID._model_cache["model-path"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_safe_when_mlx_unavailable(self):
        """When _HAS_MLX is False the method returns without touching mlx.core."""
        with patch("core.audio_lang_id._HAS_MLX", False):
            # Must not raise.
            AudioLanguageID.clear_model_cache()

        self.assertEqual(len(AudioLanguageID._model_cache), 0)


class TestClearModelCacheSafeWhenMxClearCacheRaises(unittest.TestCase):
    """clear_model_cache() silences exceptions from mx.clear_cache()."""

    def setUp(self):
        AudioLanguageID._model_cache["model-path"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_safe_when_mx_clear_cache_raises(self):
        """If mx.clear_cache() raises, clear_model_cache() must not propagate."""
        import types
        mock_mlx_pkg = types.ModuleType("mlx")
        mock_mx = MagicMock()
        mock_mx.clear_cache.side_effect = RuntimeError("Metal error")

        # Patch BOTH "mlx" and "mlx.core" so the runtime `import mlx.core` inside
        # clear_model_cache() resolves on Ubuntu where mlx is not installed.
        with patch("core.audio_lang_id._HAS_MLX", True):
            with patch.dict("sys.modules", {"mlx": mock_mlx_pkg, "mlx.core": mock_mx}):
                # Must not raise even though mx.clear_cache() does.
                AudioLanguageID.clear_model_cache()

        self.assertEqual(len(AudioLanguageID._model_cache), 0,
                         "cache должен быть пуст даже при исключении в mx.clear_cache()")


if __name__ == "__main__":
    unittest.main()
