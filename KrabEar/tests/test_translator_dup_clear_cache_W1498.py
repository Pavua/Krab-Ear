"""W1492 F1 CRIT + F2 HIGH regression tests.

F1 CRIT: translator.py had a duplicate clear_cache that shadowed the correct one;
         the shadowing version did NOT call _unavailable.clear(), so failed models
         stayed permanently blocked after a privacy-mode cache flush.

F2 HIGH: _privacy_was_on was referenced in _translate_impl before being initialised
         in __init__, causing AttributeError on the first call when privacy=True.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Path setup — allow running standalone from repo root.
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.translator import Translator  # noqa: E402


# ---------------------------------------------------------------------------
# AST helper
# ---------------------------------------------------------------------------
def _count_method_defs(source_path: str, method_name: str) -> int:
    """Return the number of top-level method definitions with *method_name* in source."""
    with open(source_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == method_name:
                count += 1
    return count


_TRANSLATOR_SRC = os.path.join(_PROJECT_ROOT, "backend", "translator.py")


class TestNoDuplicateClearCacheDefinitions(unittest.TestCase):
    """W1492 F1 CRIT — AST scan for duplicate clear_cache method."""

    def test_no_duplicate_clear_cache_definitions(self) -> None:
        count = _count_method_defs(_TRANSLATOR_SRC, "clear_cache")
        self.assertEqual(
            count,
            1,
            f"Expected exactly 1 'clear_cache' definition in translator.py, found {count}. "
            "Duplicate overrides the correct version that calls _unavailable.clear().",
        )


class TestClearCacheClearsUnavailableModels(unittest.TestCase):
    """W1492 F1 CRIT — clear_cache must reset _unavailable so failed models can be retried."""

    def test_clear_cache_clears_unavailable_models(self) -> None:
        t = Translator()
        # Simulate model load failures being tracked.
        fake_key = ("Helsinki-NLP/opus-mt-ru-es", False)
        t._unavailable.add(fake_key)
        self.assertIn(fake_key, t._unavailable, "Pre-condition: key should be in _unavailable")

        t.clear_cache()

        self.assertNotIn(
            fake_key,
            t._unavailable,
            "clear_cache() must remove entries from _unavailable; "
            "otherwise failed models stay blocked forever after privacy-mode flush.",
        )

    def test_clear_cache_clears_lru_cache(self) -> None:
        """clear_cache must also clear the in-memory LRU translation cache."""
        t = Translator()
        from backend.translator import TranslationResult
        dummy = TranslationResult(
            text="hola", status="ok",
            source_lang="ru", target_lang="es",
            mode="ru_to_es", engine="hf_marian",
        )
        key = ("ru_to_es", "neutral", "offline_default", "привет")
        t._cache[key] = dummy
        self.assertEqual(len(t._cache), 1)

        t.clear_cache()

        self.assertEqual(
            len(t._cache),
            0,
            "clear_cache() must clear the in-memory LRU cache.",
        )

    def test_clear_cache_calls_disk_cache_if_injected(self) -> None:
        """clear_cache must call .clear() on _translation_cache when it is injected."""
        t = Translator()

        class FakeDiskCache:
            cleared = False
            def clear(self):
                FakeDiskCache.cleared = True

        t._translation_cache = FakeDiskCache()  # type: ignore[assignment]
        t.clear_cache()

        self.assertTrue(
            FakeDiskCache.cleared,
            "clear_cache() must call _translation_cache.clear() when disk cache is injected.",
        )

    def test_clear_cache_tolerates_disk_cache_none(self) -> None:
        """clear_cache must not raise when _translation_cache is None (no disk cache injected)."""
        t = Translator()
        self.assertIsNone(t._translation_cache)
        try:
            t.clear_cache()  # must not raise
        except Exception as exc:
            self.fail(f"clear_cache() raised unexpectedly with _translation_cache=None: {exc}")


class TestTranslatorInitializesPrivacyWasOnAttribute(unittest.TestCase):
    """W1492 F2 HIGH — _privacy_was_on must be initialised in __init__."""

    def test_translator_initializes_privacy_was_on_attribute(self) -> None:
        t = Translator()
        self.assertTrue(
            hasattr(t, "_privacy_was_on"),
            "Translator.__init__ must initialise self._privacy_was_on to avoid AttributeError.",
        )

    def test_privacy_was_on_initial_value_is_none(self) -> None:
        """Initial value must be None so first call with privacy=True doesn't trigger clear_cache."""
        t = Translator()
        self.assertIsNone(
            t._privacy_was_on,
            "self._privacy_was_on should start as None (sentinel) to guard first-call path.",
        )


class TestTranslateFirstCallPrivacyTrueNoAttributeError(unittest.TestCase):
    """W1492 F2 HIGH — _translate_impl must not raise AttributeError when privacy=True on first call."""

    def _make_stub_result(self):
        from backend.translator import TranslationResult
        return TranslationResult(
            text="", status="empty_text", source_lang="", target_lang="",
            mode="off", engine="none",
        )

    def test_translate_first_call_privacy_true_no_attribute_error(self) -> None:
        """Calling _translate_impl directly with _privacy_mode=True on fresh Translator
        must not raise AttributeError for _privacy_was_on."""
        t = Translator()
        t._privacy_mode = True  # type: ignore[attr-defined]  # simulate privacy enabled

        # _translate_impl with empty text hits the early-return path — enough to exercise
        # the _privacy_was_on guard at the top of _translate_impl.
        try:
            result = t._translate_impl(
                text="",
                normalized_mode="off",
                network_mode="offline_default",
            )
        except AttributeError as exc:
            self.fail(
                f"_translate_impl raised AttributeError on first call with privacy=True: {exc}. "
                "self._privacy_was_on must be initialised in __init__."
            )
        # Should return empty_text result (not raise)
        self.assertEqual(result.status, "empty_text")

    def test_translate_impl_sets_privacy_was_on_after_call(self) -> None:
        """After _translate_impl is called, _privacy_was_on must reflect current privacy mode."""
        t = Translator()
        t._privacy_mode = False  # type: ignore[attr-defined]
        t._translate_impl(text="", normalized_mode="off", network_mode="offline_default")
        self.assertFalse(t._privacy_was_on)

    def test_translate_impl_privacy_true_clears_cache_once(self) -> None:
        """When privacy transitions None→True, clear_cache is called.
        When privacy stays True on subsequent calls, clear_cache is NOT called again."""
        clear_count = [0]
        t = Translator()
        original_clear = t.clear_cache

        def counting_clear():
            clear_count[0] += 1
            original_clear()

        t.clear_cache = counting_clear  # type: ignore[method-assign]

        # First call: _privacy_was_on=None, privacy_now=True → clears cache
        t._privacy_mode = True  # type: ignore[attr-defined]
        t._translate_impl(text="", normalized_mode="off", network_mode="offline_default")
        # None→True transition: should have triggered clear_cache
        self.assertEqual(
            clear_count[0],
            1,
            "clear_cache should be called once on first call when privacy=True (None→True transition).",
        )

        # Second call: _privacy_was_on=True, privacy_now=True → no additional clear
        t._translate_impl(text="", normalized_mode="off", network_mode="offline_default")
        self.assertEqual(
            clear_count[0],
            1,
            "clear_cache should NOT be called again when privacy stays True.",
        )


if __name__ == "__main__":
    unittest.main()
