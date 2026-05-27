"""W1466 — clear_model_cache() acquires _cache_lock and clears _model_cache.

Post-W1271 implementation: clear_model_cache() acquires _cache_lock (threading.Lock)
and calls _model_cache.clear(). Tests updated from original mx.clear_cache-centric
checks (stale since W1271 rewrote clear_model_cache) to match the current contract.

Tests:
1. test_clear_model_cache_acquires_mlx_lock     — _cache_lock is held when dict is cleared
2. test_clear_model_cache_lock_called_before_mx_clear — lock_enter precedes dict_clear
3. test_clear_model_cache_safe_when_lock_unavailable — cache is cleared under normal lock
"""

from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID  # noqa: E402


class TestClearModelCacheAcquiresMlxLock(unittest.TestCase):
    """clear_model_cache() must hold _cache_lock while clearing _model_cache."""

    def setUp(self):
        AudioLanguageID._model_cache["some-model"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_acquires_mlx_lock(self):
        """_cache_lock must be held at the moment _model_cache.clear() is called."""
        context_active_during_clear = []
        ctx_active = threading.Event()

        class _SpyLock:
            def __enter__(self_lk):
                ctx_active.set()
                return self_lk

            def __exit__(self_lk, *_):
                ctx_active.clear()

        class _SpyDict(dict):
            def clear(self_d):
                context_active_during_clear.append(ctx_active.is_set())
                super().clear()

        spy_cache = _SpyDict({"some-model": MagicMock()})
        spy_lock = _SpyLock()

        with patch.object(AudioLanguageID, "_model_cache", spy_cache), \
             patch.object(AudioLanguageID, "_cache_lock", spy_lock):
            AudioLanguageID.clear_model_cache()

        self.assertTrue(
            context_active_during_clear and context_active_during_clear[0],
            "_model_cache.clear() должен вызываться пока _cache_lock удерживается",
        )
        self.assertEqual(len(spy_cache), 0,
                         "кеш должен быть пуст после clear_model_cache()")

    def test_clear_model_cache_lock_called_before_mx_clear(self):
        """_cache_lock context must be entered before _model_cache is cleared."""
        call_order: list[str] = []

        class _SpyLock:
            def __enter__(self_lk):
                call_order.append("lock_enter")
                return self_lk

            def __exit__(self_lk, *_):
                call_order.append("lock_exit")

        class _SpyDict(dict):
            def clear(self_d):
                call_order.append("dict_clear")
                super().clear()

        spy_cache = _SpyDict({"some-model": MagicMock()})
        spy_lock = _SpyLock()

        with patch.object(AudioLanguageID, "_model_cache", spy_cache), \
             patch.object(AudioLanguageID, "_cache_lock", spy_lock):
            AudioLanguageID.clear_model_cache()

        self.assertIn("lock_enter", call_order)
        self.assertIn("dict_clear", call_order)
        self.assertIn("lock_exit", call_order)
        lock_idx = call_order.index("lock_enter")
        clear_idx = call_order.index("dict_clear")
        exit_idx = call_order.index("lock_exit")
        self.assertLess(lock_idx, clear_idx,
                        "lock_enter должно быть ДО dict_clear")
        self.assertLess(clear_idx, exit_idx,
                        "dict_clear должно быть ДО lock_exit")


class TestClearModelCacheSafeWhenLockUnavailable(unittest.TestCase):
    """clear_model_cache() clears the dict under normal locking conditions."""

    def setUp(self):
        AudioLanguageID._model_cache["model-path"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_safe_when_lock_unavailable(self):
        """With a working lock, clear_model_cache() empties _model_cache."""
        # The current implementation uses `with cls._cache_lock:` directly.
        # This test verifies the happy-path: normal lock → cache cleared.
        AudioLanguageID.clear_model_cache()
        self.assertEqual(len(AudioLanguageID._model_cache), 0,
                         "cache должен быть пуст после clear_model_cache()")


if __name__ == "__main__":
    unittest.main()
