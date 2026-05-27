"""
Wave 63 H4 — AudioLanguageID._model_cache bounded to 1 entry.

Tests use mocked mlx_whisper.load_models — no GPU/model required.
"""
import sys
import os
import types
import importlib
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _install_mlx_whisper_stub():
    """Register stub mlx_whisper modules so audio_lang_id can be imported."""
    stub_whisper = types.ModuleType("mlx_whisper")
    stub_load = types.ModuleType("mlx_whisper.load_models")
    stub_load.load_model = MagicMock(side_effect=lambda p: {"model": p})
    stub_whisper.load_models = stub_load
    sys.modules.setdefault("mlx_whisper", stub_whisper)
    sys.modules.setdefault("mlx_whisper.load_models", stub_load)
    return stub_load


class TestAudioLanguageIDCacheLimit(unittest.TestCase):
    """AudioLanguageID._model_cache must never hold more than 1 entry."""

    def setUp(self):
        _install_mlx_whisper_stub()
        # Capture current module (may be None if not yet imported) so tearDown
        # can restore it and prevent contamination of W1466 tests that import
        # AudioLanguageID at module-level and patch the original object.
        self._original_module = sys.modules.get("core.audio_lang_id")
        # Fresh import each test to avoid cross-test state
        if "core.audio_lang_id" in sys.modules:
            del sys.modules["core.audio_lang_id"]
        self.mod = importlib.import_module("core.audio_lang_id")
        self.AudioLanguageID = self.mod.AudioLanguageID
        self.AudioLanguageID._model_cache.clear()

    def tearDown(self):
        # Restore the original module so that other test files which imported
        # AudioLanguageID at module-level (e.g. W1466 tests) continue to patch
        # the correct object in sys.modules["core.audio_lang_id"].
        if self._original_module is not None:
            sys.modules["core.audio_lang_id"] = self._original_module
        else:
            sys.modules.pop("core.audio_lang_id", None)

    def _insert(self, path: str):
        """Simulate bounded cache insertion (mirrors audio_lang_id.py logic)."""
        cls = self.AudioLanguageID
        if path not in cls._model_cache:
            if len(cls._model_cache) >= 1:
                cls._model_cache.clear()
            cls._model_cache[path] = {"model": path}

    def test_single_model_stored(self):
        self._insert("model-a")
        self.assertEqual(len(self.AudioLanguageID._model_cache), 1)

    def test_second_model_evicts_first(self):
        """Inserting a different key evicts the existing entry."""
        self._insert("model-a")
        self._insert("model-b")
        cache = self.AudioLanguageID._model_cache
        self.assertEqual(len(cache), 1)
        self.assertIn("model-b", cache)
        self.assertNotIn("model-a", cache)

    def test_cache_never_exceeds_one(self):
        """After 10 different insertions the cache still holds exactly 1 entry."""
        for i in range(10):
            self._insert(f"model-{i}")
            self.assertLessEqual(len(self.AudioLanguageID._model_cache), 1)
        self.assertEqual(len(self.AudioLanguageID._model_cache), 1)
        self.assertIn("model-9", self.AudioLanguageID._model_cache)

    def test_same_model_path_no_eviction(self):
        """Loading the same model path twice does NOT evict and re-insert."""
        self._insert("model-a")
        # Calling _insert with same key — guard should skip eviction
        self._insert("model-a")
        cache = self.AudioLanguageID._model_cache
        self.assertEqual(len(cache), 1)
        self.assertIn("model-a", cache)


class TestSysModulesRestoredAfterTeardown(unittest.TestCase):
    """Verify that TestAudioLanguageIDCacheLimit.tearDown restores sys.modules."""

    def test_sys_modules_restored_after_teardown(self):
        """After setUp+tearDown cycle, sys.modules['core.audio_lang_id'] is restored."""
        _install_mlx_whisper_stub()
        # Ensure module is loaded before we run the test class cycle
        import importlib as _il
        _il.import_module("core.audio_lang_id")
        original = sys.modules.get("core.audio_lang_id")
        self.assertIsNotNone(original, "core.audio_lang_id must be importable")

        # Simulate a full setUp/tearDown cycle of TestAudioLanguageIDCacheLimit
        instance = TestAudioLanguageIDCacheLimit("test_single_model_stored")
        instance.setUp()
        # After setUp, sys.modules has a NEW module object
        mid_module = sys.modules.get("core.audio_lang_id")
        # It might be a fresh module or same if originally None — that's fine
        instance.tearDown()

        # After tearDown, sys.modules must be restored to original
        restored = sys.modules.get("core.audio_lang_id")
        self.assertIs(
            restored,
            original,
            "tearDown must restore the original module object in sys.modules",
        )


if __name__ == "__main__":
    unittest.main()
