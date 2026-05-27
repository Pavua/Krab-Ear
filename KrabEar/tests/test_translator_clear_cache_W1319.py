"""W1319 — Translator.clear_cache() wipes both in-memory and disk-persistent layers.

Tests:
  - test_clear_cache_wipes_memory
  - test_clear_cache_wipes_disk_persistent
  - test_privacy_mode_toggle_wipes_both_caches
"""

from __future__ import annotations

import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator, TranslationResult


def _make_ok_result(text: str = "translated") -> TranslationResult:
    return TranslationResult(
        text=text,
        status="ok",
        source_lang="ru",
        target_lang="es",
        mode="ru_to_es",
        engine="hf_marian",
    )


class TranslatorClearCacheMemoryTestCase(unittest.TestCase):
    """clear_cache() wipes in-memory LRU cache."""

    def test_clear_cache_wipes_memory(self) -> None:
        """After clear_cache(), the in-memory _cache OrderedDict is empty."""
        translator = Translator()
        # Populate _cache directly to avoid needing a real pipeline.
        key = ("ru_to_es", "neutral", "offline_default", "Привет")
        translator._cache[key] = _make_ok_result("Hola")
        self.assertEqual(len(translator._cache), 1, "precondition: cache has 1 entry")

        translator.clear_cache()

        self.assertEqual(
            len(translator._cache),
            0,
            "clear_cache() must wipe in-memory _cache",
        )

    def test_clear_cache_no_disk_layer_is_noop(self) -> None:
        """clear_cache() is safe when _translation_cache is None (not injected)."""
        translator = Translator()
        # W1429: _translation_cache is now explicitly declared as None in __init__.
        # The attribute exists but is None — no disk layer active.
        self.assertIsNone(translator._translation_cache)
        translator._cache[("m", "n", "o", "p")] = _make_ok_result()

        # Must not raise even without a disk layer.
        translator.clear_cache()

        self.assertEqual(len(translator._cache), 0)


class TranslatorClearCacheDiskTestCase(unittest.TestCase):
    """clear_cache() also calls TranslationCache.clear() (disk layer)."""

    def test_clear_cache_wipes_disk_persistent(self) -> None:
        """When _translation_cache is injected, clear_cache() calls its .clear()."""
        translator = Translator()
        mock_disk_cache = MagicMock()
        # Late-inject disk cache (W1190 pattern).
        translator._translation_cache = mock_disk_cache  # type: ignore[attr-defined]

        # Populate in-memory layer too.
        translator._cache[("ru_to_es", "neutral", "offline_default", "test")] = _make_ok_result()

        translator.clear_cache()

        # Both layers cleared.
        self.assertEqual(len(translator._cache), 0, "in-memory cache must be wiped")
        mock_disk_cache.clear.assert_called_once_with()

    def test_clear_cache_disk_exception_is_swallowed(self) -> None:
        """clear_cache() continues silently even if disk .clear() raises."""
        translator = Translator()
        mock_disk_cache = MagicMock()
        mock_disk_cache.clear.side_effect = OSError("disk full")
        translator._translation_cache = mock_disk_cache  # type: ignore[attr-defined]
        translator._cache[("x", "y", "z", "w")] = _make_ok_result()

        # Should not propagate the OSError.
        translator.clear_cache()

        # In-memory still cleared despite disk error.
        self.assertEqual(len(translator._cache), 0)


class TranslatorPrivacyModeClearTestCase(unittest.TestCase):
    """_check_privacy_mode_changed() wipes both layers on privacy_mode transition."""

    def test_privacy_mode_toggle_wipes_both_caches(self) -> None:
        """Transition False→True clears both in-memory and disk layers."""
        translator = Translator()
        mock_disk_cache = MagicMock()
        translator._translation_cache = mock_disk_cache  # type: ignore[attr-defined]

        # Seed in-memory cache.
        translator._cache[("ru_to_es", "neutral", "offline_default", "секрет")] = _make_ok_result("secreto")
        self.assertEqual(len(translator._cache), 1)

        # First call initialises tracking — no clear triggered.
        translator._check_privacy_mode_changed(False)
        self.assertEqual(len(translator._cache), 1, "first call must not clear cache")
        mock_disk_cache.clear.assert_not_called()

        # Second call with same value — no transition — no clear.
        translator._check_privacy_mode_changed(False)
        self.assertEqual(len(translator._cache), 1, "no transition: cache must remain")
        mock_disk_cache.clear.assert_not_called()

        # Transition False → True — both layers must be cleared.
        translator._check_privacy_mode_changed(True)
        self.assertEqual(
            len(translator._cache),
            0,
            "privacy_mode transition must wipe in-memory cache",
        )
        mock_disk_cache.clear.assert_called_once_with()

    def test_privacy_mode_true_to_false_also_wipes(self) -> None:
        """Transition True→False also triggers a clear (reverse direction)."""
        translator = Translator()
        mock_disk_cache = MagicMock()
        translator._translation_cache = mock_disk_cache  # type: ignore[attr-defined]
        translator._cache[("k1", "k2", "k3", "k4")] = _make_ok_result()

        translator._check_privacy_mode_changed(True)   # init
        translator._cache[("a1", "a2", "a3", "a4")] = _make_ok_result()  # re-seed
        translator._check_privacy_mode_changed(False)  # transition True→False

        self.assertEqual(len(translator._cache), 0)
        mock_disk_cache.clear.assert_called_once_with()

    def test_privacy_mode_no_transition_no_clear(self) -> None:
        """Stable privacy_mode value never triggers clear_cache."""
        translator = Translator()
        mock_disk_cache = MagicMock()
        translator._translation_cache = mock_disk_cache  # type: ignore[attr-defined]
        translator._cache[("x", "y", "z", "w")] = _make_ok_result()

        for _ in range(5):
            translator._check_privacy_mode_changed(True)

        # Cache unchanged, disk never cleared.
        self.assertEqual(len(translator._cache), 1)
        mock_disk_cache.clear.assert_not_called()


if __name__ == "__main__":
    unittest.main()
