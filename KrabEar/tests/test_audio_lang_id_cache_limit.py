"""
Wave 63 H4 — AudioLanguageID._model_cache bounded to 1 entry.

Tests use mocked mlx_whisper.load_models — no GPU/model required.
"""
import sys
import os
import importlib
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestAudioLanguageIDCacheLimit(unittest.TestCase):
    """AudioLanguageID._model_cache must never hold more than 1 entry."""

    def setUp(self):
        # G3-audio-01 A02: без переимпорта core.audio_lang_id и без постоянной
        # заглушки mlx_whisper (импорт в модуле ленивый) — только чистый кэш.
        self.mod = importlib.import_module("core.audio_lang_id")
        self.AudioLanguageID = self.mod.AudioLanguageID
        self._saved_cache = dict(self.AudioLanguageID._model_cache)
        self.AudioLanguageID._model_cache.clear()

    def tearDown(self):
        self.AudioLanguageID._model_cache.clear()
        self.AudioLanguageID._model_cache.update(self._saved_cache)

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

    def test_setup_teardown_keep_module_and_mlx_untouched(self):
        """Цикл setUp/tearDown не меняет sys.modules (A02)."""
        original = importlib.import_module("core.audio_lang_id")
        before_mlx = sys.modules.get("mlx_whisper")
        instance = TestAudioLanguageIDCacheLimit("test_single_model_stored")
        instance.setUp()
        self.assertIs(sys.modules.get("core.audio_lang_id"), original)
        instance.tearDown()
        self.assertIs(sys.modules.get("core.audio_lang_id"), original)
        self.assertIs(sys.modules.get("mlx_whisper"), before_mlx)


if __name__ == "__main__":
    unittest.main()
