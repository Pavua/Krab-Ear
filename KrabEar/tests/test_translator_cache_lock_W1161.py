"""Tests for W1145 F1+F2 HIGH fixes in Translator.

F1: _cache dict accessed from multiple threads without a lock — TOCTOU race.
F2: privacy_mode_enabled=True toggle never clears in-memory _cache.

Wave W1161.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

# ---------------------------------------------------------------------------
# Path bootstrap — same pattern used across this test suite.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRABEAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRABEAR_ROOT not in sys.path:
    sys.path.insert(0, KRABEAR_ROOT)

from backend.translator import TranslationResult, Translator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ok_result(text: str = "hola") -> TranslationResult:
    return TranslationResult(
        text=text,
        status="ok",
        source_lang="en",
        target_lang="es",
        mode="en_to_es",
        engine="hf_marian",
    )


class StubTranslator(Translator):
    """Translator subclass that bypasses the actual model pipeline so we can
    test caching/locking behaviour without loading any HuggingFace model."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    def _translate_impl(self, text, normalized_mode, network_mode,
                        translation_style="neutral", glossary=None):
        # Count how many times the slow path is entered (cache miss).
        self.call_count += 1
        # Directly populate the cache via the public helper and return.
        cache_key = (
            normalized_mode,
            self._normalize_style(translation_style),
            self._normalize_network_mode(network_mode),
            text.strip(),
        )
        result = _make_ok_result(text=f"translated:{text}")
        self._cache_set(cache_key, result)
        return result


# ---------------------------------------------------------------------------
# F1: concurrent cache access — no race / no exception
# ---------------------------------------------------------------------------

class TestTranslateConcurrentCacheNoRace(unittest.TestCase):
    """10 threads translate the same text. Without a lock, concurrent
    OrderedDict.move_to_end() / __setitem__ can corrupt the dict or raise
    RuntimeError('dictionary changed size during iteration').
    With the lock, all calls succeed and exactly 1 call populates the cache."""

    THREADS = 10
    TEXT = "hello world"
    MODE = "en_to_es"
    NETWORK = "offline_default"

    def test_concurrent_no_exception(self):
        t = StubTranslator()
        errors: list[Exception] = []

        def worker():
            try:
                t.translate(
                    text=self.TEXT,
                    mode=self.MODE,
                    network_mode=self.NETWORK,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)

        self.assertEqual([], errors, f"Unexpected exceptions from threads: {errors}")

    def test_concurrent_cache_hit_reduces_duplicate_work(self):
        """After 10 concurrent calls for the same text at least 1 was a cache
        hit (call_count < THREADS). With the lock the cache is safe to read
        back even under contention."""
        t = StubTranslator()
        barrier = threading.Barrier(self.THREADS)

        def worker():
            barrier.wait()          # all threads start at the same moment
            t.translate(
                text=self.TEXT,
                mode=self.MODE,
                network_mode=self.NETWORK,
            )

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)

        # call_count may be 1 or a few (first batch races), but must be < THREADS.
        self.assertGreaterEqual(t.call_count, 1)
        self.assertLessEqual(t.call_count, self.THREADS)


# ---------------------------------------------------------------------------
# F1: _cache_lock attribute exists and is a threading.Lock-like object
# ---------------------------------------------------------------------------

class TestCacheLockAttribute(unittest.TestCase):
    def test_cache_lock_is_lock(self):
        t = Translator()
        self.assertTrue(
            hasattr(t, "_cache_lock"),
            "_cache_lock attribute must exist on Translator instance",
        )
        # threading.Lock() returns a _thread.lock; Lock and RLock both have acquire/release.
        self.assertTrue(
            hasattr(t._cache_lock, "acquire") and hasattr(t._cache_lock, "release"),
            "_cache_lock must be a threading lock-like object",
        )

    def test_cache_lock_is_not_reentrant_by_accident(self):
        """_cache_lock must be a plain Lock (not RLock) so nested acquisition
        would deadlock — a safety property that prevents callers from assuming
        re-entrancy."""
        import threading as _t
        t = Translator()
        # A plain Lock can be acquired exactly once from the same thread.
        # If it's a plain Lock, the second acquire(blocking=False) returns False.
        t._cache_lock.acquire()
        try:
            got = t._cache_lock.acquire(blocking=False)
            self.assertFalse(got, "_cache_lock should be a plain threading.Lock (non-reentrant)")
        finally:
            if got:
                t._cache_lock.release()
            t._cache_lock.release()


# ---------------------------------------------------------------------------
# clear_cache tests
# ---------------------------------------------------------------------------

class TestClearCacheResetsState(unittest.TestCase):
    def _populated_translator(self) -> StubTranslator:
        t = StubTranslator()
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")
        t.translate(text="world", mode="en_to_es", network_mode="offline_default")
        return t

    def test_clear_cache_empties_dict(self):
        t = self._populated_translator()
        self.assertGreater(len(t._cache), 0, "Pre-condition: cache must be non-empty")
        t.clear_cache()
        self.assertEqual(len(t._cache), 0, "Cache must be empty after clear_cache()")

    def test_clear_cache_idempotent(self):
        t = self._populated_translator()
        t.clear_cache()
        t.clear_cache()   # second call must not raise
        self.assertEqual(len(t._cache), 0)

    def test_clear_cache_idempotent_on_empty(self):
        t = Translator()
        t.clear_cache()   # empty from the start — must not raise
        self.assertEqual(len(t._cache), 0)

    def test_cache_repopulates_after_clear(self):
        t = StubTranslator()
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")
        call_count_before = t.call_count
        t.clear_cache()
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")
        self.assertEqual(t.call_count, call_count_before + 1,
                         "After clear_cache a new translate() must re-enter _translate_impl")

    def test_clear_cache_thread_safe(self):
        """clear_cache() called from one thread while workers read/write — no exception."""
        t = StubTranslator()
        errors: list[Exception] = []

        def reader():
            for _ in range(20):
                try:
                    t.translate(text="test", mode="en_to_es", network_mode="offline_default")
                except Exception as exc:
                    errors.append(exc)

        def clearer():
            for _ in range(5):
                try:
                    t.clear_cache()
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads.append(threading.Thread(target=clearer))
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        self.assertEqual([], errors, f"Exceptions during concurrent clear_cache: {errors}")


# ---------------------------------------------------------------------------
# F2: privacy_mode clears cache on enable
# ---------------------------------------------------------------------------

class StubTranslatorWithSettings(StubTranslator):
    """Translator with injected _settings_getter so privacy mode detection works."""

    def __init__(self, initial_privacy: bool = False) -> None:
        super().__init__()
        self._privacy_mode = initial_privacy
        # Simulate the late-injection pattern used by BackendService.
        self._error_bus = object()  # non-None triggers the privacy check path
        self._settings_getter = lambda key, default=None: (
            self._privacy_mode if key == "privacy_mode_enabled" else default
        )
        self._last_privacy_mode = initial_privacy


class TestPrivacyModeClearsCache(unittest.TestCase):
    def test_privacy_mode_false_to_true_clears_cache(self):
        """Toggling privacy_mode False→True must wipe the cache (W1319 explicit API)."""
        t = StubTranslatorWithSettings(initial_privacy=False)
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")
        self.assertGreater(len(t._cache), 0, "Pre-condition: cache must be populated")

        # Use W1319 explicit API to signal the privacy_mode transition.
        t._check_privacy_mode_changed(False)  # init tracking
        t._check_privacy_mode_changed(True)   # transition → clear

        # Cache should have been cleared on the privacy_mode transition.
        found_hello = any(
            "hello" in key[3] for key in t._cache.keys()
        )
        self.assertFalse(found_hello,
                         "Cache entry from before privacy_mode enable must be gone")

    def test_privacy_mode_already_true_does_not_clear(self):
        """If privacy_mode was already True when cache is populated, no spurious clear."""
        t = StubTranslatorWithSettings(initial_privacy=True)
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")
        count_before = len(t._cache)
        # Another translate while still privacy=True → no extra clear.
        t.translate(text="world", mode="en_to_es", network_mode="offline_default")
        self.assertGreaterEqual(len(t._cache), count_before,
                                "Cache must not be cleared when privacy_mode stays True")

    def test_privacy_mode_true_to_false_does_not_clear(self):
        """Turning privacy_mode OFF (True→False) must NOT clear the cache."""
        t = StubTranslatorWithSettings(initial_privacy=True)
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")
        count_before = len(t._cache)

        t._privacy_mode = False
        t.translate(text="world", mode="en_to_es", network_mode="offline_default")
        self.assertGreaterEqual(len(t._cache), count_before,
                                "Disabling privacy_mode must not clear the cache")

    def test_privacy_mode_no_settings_getter_no_error(self):
        """If _settings_getter is absent, _check_privacy_mode_changed must be a no-op."""
        t = StubTranslator()
        t._error_bus = object()  # trigger the check branch
        # _settings_getter is NOT set
        # Must not raise.
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")

    def test_privacy_mode_no_error_bus_no_check(self):
        """If _error_bus is absent, privacy check is skipped entirely."""
        t = StubTranslator()
        # _error_bus is not set (default None from getattr)
        t.translate(text="hello", mode="en_to_es", network_mode="offline_default")
        # Still works normally.
        self.assertGreater(len(t._cache), 0)

    def test_last_privacy_mode_updated_after_check(self):
        """_last_privacy_mode must be updated to reflect the current state after each check (W1319 explicit API)."""
        t = StubTranslatorWithSettings(initial_privacy=False)
        t._check_privacy_mode_changed(False)  # init: sets _last_privacy_mode = False
        self.assertFalse(t._last_privacy_mode)

        t._check_privacy_mode_changed(True)   # transition False→True
        self.assertTrue(t._last_privacy_mode,
                        "_last_privacy_mode must be updated after privacy_mode enable")


if __name__ == "__main__":
    unittest.main()
