"""W1416 — audio_lang_id.clear_model_cache() calls mx.clear_cache() (W1405 F2 MED).

Tests:
1. test_clear_model_cache_calls_mx_clear_cache_when_mlx_available
2. test_clear_model_cache_safe_when_mlx_unavailable
3. test_clear_model_cache_safe_when_mx_clear_cache_raises

W1752 xdist fix: _MLX_AVAILABLE guard wraps find_spec() in try/except ValueError
so collection-time crashes are prevented when another test file has left a
MagicMock (no __spec__) in sys.modules["mlx"].  importlib.util.find_spec()
raises ValueError when the entry in sys.modules has no __spec__ attribute.
"""

from __future__ import annotations

import importlib.util
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID, _HAS_MLX  # noqa: E402

# W1749/W1752: determine MLX availability at import time so skipUnless works.
# _HAS_MLX is set at core.audio_lang_id module load by trying `import mlx.core`.
# Using importlib.util.find_spec is safer for collection-time checking —
# it does NOT attempt to dlopen the shared library, so Ubuntu CI (no libmlx.so)
# never triggers an ImportError during test collection.
#
# W1752: wrap find_spec() in try/except ValueError.  When another test file
# installed sys.modules["mlx"] = MagicMock() and that file was collected first
# in the same xdist worker, the conftest stub-purge fires *after tests*, not
# after collection.  find_spec("mlx") raises ValueError: mlx.__spec__ is not set
# when sys.modules["mlx"] is a MagicMock.  Catching ValueError and treating it
# as "mlx not available" is the correct behaviour — we cannot use the real MLX
# in this process state anyway.


def _check_mlx_available() -> bool:
    if not _HAS_MLX:
        return False
    try:
        return importlib.util.find_spec("mlx") is not None
    except (ValueError, ModuleNotFoundError):
        return False


_MLX_AVAILABLE: bool = _check_mlx_available()


class TestClearModelCacheCallsMxClearCache(unittest.TestCase):
    """clear_model_cache() calls mx.clear_cache() when MLX is available."""

    def setUp(self):
        # Seed the cache with a dummy entry so there is something to clear.
        AudioLanguageID._model_cache["some-model"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    @unittest.skipUnless(_MLX_AVAILABLE, "requires MLX runtime (mlx.core not installed)")
    def test_clear_model_cache_calls_mx_clear_cache_when_mlx_available(self):
        """When _HAS_MLX is True, clear_model_cache() must call mx.clear_cache().

        W1749: guarded with @skipUnless(_MLX_AVAILABLE) so the test SKIPS
        (rather than fails) on runners where mlx is not installed.  The test
        name explicitly states "when_mlx_available" — skipping on no-mlx is the
        semantically correct behaviour, not a workaround.

        The prior mock-only approach (try/except ImportError) evaded the
        full-suite xdist run: when another test in the same worker had already
        cleared "mlx.core" from sys.modules via the conftest stub-purge,
        `import mlx.core as _mx_rt` inside clear_model_cache() re-imported a
        FRESH object.  patch.object(_real_mx, "clear_cache") patched the OLD
        object, so the new import's clear_cache was never the patched spy →
        "clear_cache called 0 times".  skipUnless avoids the spy entirely on
        no-MLX runners; on MLX runners the real mlx.core stays in sys.modules
        and the patch.object approach is reliable.
        """
        import mlx.core as _real_mx
        # Ensure mlx.core is pinned in sys.modules before patching so that
        # `import mlx.core as _mx_rt` inside clear_model_cache() resolves to
        # the same object we patched.
        with patch("core.audio_lang_id._HAS_MLX", True), \
             patch.dict("sys.modules", {"mlx.core": _real_mx}), \
             patch.object(_real_mx, "clear_cache") as mock_clear:
            AudioLanguageID.clear_model_cache()
            mock_clear.assert_called_once()

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
