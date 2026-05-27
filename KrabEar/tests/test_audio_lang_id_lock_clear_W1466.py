"""W1466 — clear_model_cache() wraps mx.clear_cache() in mlx_lock() (W1462 F2 MED).

Tests:
1. test_clear_model_cache_acquires_mlx_lock   — mlx_lock() is acquired before mx.clear_cache()
2. test_clear_model_cache_safe_when_lock_unavailable — if mlx_lock raises, method does not propagate
"""

from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch, call

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID  # noqa: E402


class TestClearModelCacheAcquiresMlxLock(unittest.TestCase):
    """clear_model_cache() must hold mlx_lock() while calling mx.clear_cache()."""

    def setUp(self):
        AudioLanguageID._model_cache["some-model"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_acquires_mlx_lock(self):
        """mx.clear_cache() must be called inside the mlx_lock() context manager."""
        # Track whether the context manager was active when clear_cache ran.
        context_active_during_clear = []
        ctx_active = threading.Event()

        class _TrackingLockCtx:
            """Context manager that sets/clears ctx_active flag."""
            def __enter__(self_cm):
                ctx_active.set()
                return self_cm

            def __exit__(self_cm, *_):
                ctx_active.clear()

        fake_mx = MagicMock()

        def _spy_clear_cache():
            context_active_during_clear.append(ctx_active.is_set())

        fake_mx.clear_cache.side_effect = _spy_clear_cache

        with patch("core.audio_lang_id._HAS_MLX", True), \
             patch("core.audio_lang_id.mx", fake_mx), \
             patch("core.audio_lang_id.mlx_lock", return_value=_TrackingLockCtx()):
            AudioLanguageID.clear_model_cache()

        fake_mx.clear_cache.assert_called_once()
        self.assertTrue(
            context_active_during_clear and context_active_during_clear[0],
            "mx.clear_cache() должен вызываться пока mlx_lock() удерживается",
        )
        self.assertEqual(len(AudioLanguageID._model_cache), 0)

    def test_clear_model_cache_lock_called_before_mx_clear(self):
        """mlx_lock context must be entered before mx.clear_cache is invoked."""
        call_order: list[str] = []

        class _TrackingLockCtx:
            def __enter__(self_cm):
                call_order.append("lock_enter")
                return self_cm

            def __exit__(self_cm, *_):
                call_order.append("lock_exit")

        fake_mx = MagicMock()
        fake_mx.clear_cache.side_effect = lambda: call_order.append("mx_clear_cache")

        with patch("core.audio_lang_id._HAS_MLX", True), \
             patch("core.audio_lang_id.mx", fake_mx), \
             patch("core.audio_lang_id.mlx_lock", return_value=_TrackingLockCtx()):
            AudioLanguageID.clear_model_cache()

        self.assertIn("lock_enter", call_order)
        self.assertIn("mx_clear_cache", call_order)
        self.assertIn("lock_exit", call_order)
        lock_idx = call_order.index("lock_enter")
        clear_idx = call_order.index("mx_clear_cache")
        exit_idx = call_order.index("lock_exit")
        self.assertLess(lock_idx, clear_idx,
                        "lock_enter должно быть ДО mx_clear_cache")
        self.assertLess(clear_idx, exit_idx,
                        "mx_clear_cache должно быть ДО lock_exit")


class TestClearModelCacheSafeWhenLockUnavailable(unittest.TestCase):
    """clear_model_cache() silences exceptions from mlx_lock() itself."""

    def setUp(self):
        AudioLanguageID._model_cache["model-path"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_safe_when_lock_unavailable(self):
        """If mlx_lock() raises (e.g. threading error), method must not propagate."""

        class _FailingLock:
            def __enter__(self):
                raise RuntimeError("lock unavailable")

            def __exit__(self, *_):
                pass

        fake_mx = MagicMock()

        with patch("core.audio_lang_id._HAS_MLX", True), \
             patch("core.audio_lang_id.mx", fake_mx), \
             patch("core.audio_lang_id.mlx_lock", return_value=_FailingLock()):
            # Must not raise.
            AudioLanguageID.clear_model_cache()

        # Python dict is cleared even if MLX lock fails.
        self.assertEqual(len(AudioLanguageID._model_cache), 0,
                         "cache должен быть пуст даже при ошибке mlx_lock()")


if __name__ == "__main__":
    unittest.main()
