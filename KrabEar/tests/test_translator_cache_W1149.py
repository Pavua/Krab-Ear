"""Tests for W1145 F1+F2: Translator._cache RLock + privacy_mode clears cache.

W1145 F1 HIGH: OrderedDict non-atomic read+move_to_end races with live_subs background thread.
W1145 F2 HIGH: privacy_mode switch never clears in-RAM LRU cache.
"""

import sys
import os
import threading
import unittest
from dataclasses import dataclass
from collections import OrderedDict

# Resolve project root so backend.* imports work when run standalone.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _candidate in [
    os.path.join(_PROJECT_ROOT, "KrabEar"),
    os.path.join(os.path.dirname(_HERE), ".."),
]:
    _candidate = os.path.abspath(_candidate)
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)


from backend.translator import Translator, TranslationResult  # noqa: E402


class TestTranslatorCacheLockPresent(unittest.TestCase):
    """W1145 F1: _cache_lock attribute must be an RLock."""

    def test_cache_lock_is_rlock(self):
        t = Translator()
        self.assertTrue(
            hasattr(t, "_cache_lock"),
            "Translator must have _cache_lock attribute",
        )
        # RLock instances don't expose a public class but do have acquire/release
        lock = t._cache_lock
        self.assertTrue(
            callable(getattr(lock, "acquire", None)) and callable(getattr(lock, "release", None)),
            "_cache_lock must be a lock-like object with acquire/release",
        )

    def test_cache_lock_is_reentrant(self):
        """RLock must allow re-entry from the same thread (would deadlock with plain Lock)."""
        t = Translator()
        acquired = []
        with t._cache_lock:
            with t._cache_lock:  # reentrant acquire — must not deadlock
                acquired.append(True)
        self.assertEqual(acquired, [True], "RLock must allow reentrant acquisition")


class TestTranslatorCacheConcurrentNoRace(unittest.TestCase):
    """W1145 F1: concurrent _cache_get/_cache_set must not raise."""

    def setUp(self):
        self.translator = Translator()
        # Pre-populate cache with dummy entries via _cache_set directly.
        for i in range(50):
            key = (f"mode_{i}", "neutral", "offline_default", f"text_{i}")
            result = TranslationResult(
                text=f"translated_{i}",
                status="ok",
                source_lang="en",
                target_lang="ru",
                mode=f"mode_{i}",
                engine="test",
            )
            self.translator._cache_set(key, result)

    def test_concurrent_cache_operations_no_exception(self):
        errors = []

        def reader():
            for i in range(100):
                key = (f"mode_{i % 50}", "neutral", "offline_default", f"text_{i % 50}")
                try:
                    self.translator._cache_get(key)
                except Exception as exc:
                    errors.append(exc)

        def writer():
            for i in range(100):
                key = (f"mode_new_{i}", "neutral", "offline_default", f"text_new_{i}")
                result = TranslationResult(
                    text=f"new_translated_{i}",
                    status="ok",
                    source_lang="ru",
                    target_lang="es",
                    mode="ru_to_es",
                    engine="test",
                )
                try:
                    self.translator._cache_set(key, result)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads += [threading.Thread(target=writer) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)

        self.assertEqual(
            errors,
            [],
            f"Concurrent cache operations raised exceptions: {errors}",
        )


class TestTranslatorPrivacyModeClearsCache(unittest.TestCase):
    """W1145 F2: transitioning privacy_mode True must wipe in-RAM cache."""

    def _make_dummy_result(self, i: int) -> TranslationResult:
        return TranslationResult(
            text=f"translated_{i}",
            status="ok",
            source_lang="en",
            target_lang="ru",
            mode="en_to_ru",
            engine="test",
        )

    def test_clear_cache_empties_cache_and_unavailable(self):
        t = Translator()
        for i in range(10):
            key = ("en_to_ru", "neutral", "offline_default", f"text_{i}")
            t._cache_set(key, self._make_dummy_result(i))
        t._unavailable.add(("some_model", False))

        self.assertEqual(len(t._cache), 10)
        self.assertEqual(len(t._unavailable), 1)

        t.clear_cache()

        self.assertEqual(len(t._cache), 0, "clear_cache() must empty _cache")
        self.assertEqual(len(t._unavailable), 0, "clear_cache() must empty _unavailable")

    def test_privacy_transition_clears_cache_via_translate_impl(self):
        """When _privacy_mode flips False→True, _translate_impl must purge cache."""
        t = Translator()
        # Pre-populate cache
        for i in range(5):
            key = ("en_to_ru", "neutral", "offline_default", f"secret_text_{i}")
            t._cache_set(key, self._make_dummy_result(i))

        self.assertEqual(len(t._cache), 5)

        # Simulate privacy_mode = False (was off)
        t._privacy_mode = False
        t._privacy_was_on = False

        # Call _translate_impl with an empty text (returns early) but still runs transition check.
        t._translate_impl(
            text="",
            normalized_mode="off",
            network_mode="offline_default",
        )
        # Cache should still be populated (no transition)
        self.assertEqual(len(t._cache), 5, "No transition: cache must remain intact")

        # Now flip privacy_mode True → transition fires
        t._privacy_mode = True
        t._translate_impl(
            text="",
            normalized_mode="off",
            network_mode="offline_default",
        )
        self.assertEqual(len(t._cache), 0, "privacy_mode True transition must clear cache")
        self.assertTrue(t._privacy_was_on, "_privacy_was_on must be True after transition")

    def test_privacy_already_on_no_double_clear(self):
        """When privacy_mode stays True (no new transition), cache is not cleared again."""
        t = Translator()
        t._privacy_mode = True
        t._privacy_was_on = True  # already was on — no transition

        # Populate after the flag is already on
        for i in range(3):
            key = ("en_to_ru", "neutral", "offline_default", f"text_{i}")
            t._cache_set(key, self._make_dummy_result(i))

        self.assertEqual(len(t._cache), 3)

        t._translate_impl(
            text="",
            normalized_mode="off",
            network_mode="offline_default",
        )
        # No new transition: cache must remain (was already on)
        self.assertEqual(len(t._cache), 3, "Stable privacy_mode=True must not clear cache again")


class TestTranslatorNormalCachePersists(unittest.TestCase):
    """Normal (non-privacy) cache path: entries persist between calls."""

    def test_cache_set_then_get_returns_same_result(self):
        t = Translator()
        key = ("ru_to_es", "neutral", "offline_default", "привет мир")
        result = TranslationResult(
            text="hola mundo",
            status="ok",
            source_lang="ru",
            target_lang="es",
            mode="ru_to_es",
            engine="hf_marian",
        )
        t._cache_set(key, result)
        retrieved = t._cache_get(key)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.text, "hola mundo")
        self.assertEqual(retrieved.status, "ok")

    def test_cache_evicts_oldest_at_capacity(self):
        t = Translator()
        t._cache_capacity = 5
        for i in range(6):
            key = ("ru_to_es", "neutral", "offline_default", f"text_{i}")
            result = TranslationResult(
                text=f"trans_{i}",
                status="ok",
                source_lang="ru",
                target_lang="es",
                mode="ru_to_es",
                engine="hf_marian",
            )
            t._cache_set(key, result)

        self.assertEqual(len(t._cache), 5, "Cache must not exceed capacity")
        # Oldest entry (text_0) should have been evicted
        evicted_key = ("ru_to_es", "neutral", "offline_default", "text_0")
        self.assertIsNone(t._cache_get(evicted_key), "Oldest entry must be evicted")

    def test_cache_miss_returns_none(self):
        t = Translator()
        result = t._cache_get(("nonexistent", "mode", "offline_default", "text"))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
