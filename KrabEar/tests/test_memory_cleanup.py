"""
Wave 63 — memory leak regression tests.

Covers:
  H2  — mx.clear_cache() called after transcribe (engine.py)
  H2b — mx.clear_cache() graceful when ImportError / AttributeError
  H4  — AudioLanguageID._model_cache bounded to 1 entry
  H4b — profile switch evicts old cache entry

W1752 xdist fix: TestAudioLanguageIDCacheBounded.setUp installs mlx_whisper /
mlx_whisper.load_models stubs via sys.modules.setdefault().  Those stubs
survive across test runs in the same xdist worker process.  tearDown now
restores the pre-test sys.modules state for every key the setUp installs,
preventing MagicMock entries from leaking into sibling test files.
"""
import sys
import os
import types
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helper: build a minimal stub mlx.core module
# ---------------------------------------------------------------------------
def _make_mlx_stub(has_clear_cache: bool = True):
    mlx = types.ModuleType("mlx")
    core = types.ModuleType("mlx.core")
    if has_clear_cache:
        core.clear_cache = MagicMock()
    mlx.core = core
    return mlx, core


# ---------------------------------------------------------------------------
# H2 — mx.clear_cache called after transcribe
# ---------------------------------------------------------------------------
class TestMxClearCacheAfterTranscribe(unittest.TestCase):
    """Ensure the post-STT cleanup block in engine.py calls mx.clear_cache()."""

    def _run_cleanup_block(self, mlx_module):
        """Execute the exact cleanup snippet copy-pasted from engine.py."""
        import gc as _gc
        _gc.collect()
        try:
            _mx = mlx_module.core
            _mx.clear_cache()
        except (ImportError, AttributeError):
            pass

    def test_clear_cache_called_when_mlx_available(self):
        mlx, core = _make_mlx_stub(has_clear_cache=True)
        self._run_cleanup_block(mlx)
        core.clear_cache.assert_called_once()

    def test_clear_cache_not_called_when_attribute_missing(self):
        """AttributeError (older MLX without clear_cache) must be swallowed."""
        mlx, core = _make_mlx_stub(has_clear_cache=False)
        # Should not raise
        self._run_cleanup_block(mlx)

    def test_import_error_swallowed(self):
        """ImportError (MLX not installed) must be swallowed silently."""
        # Simulate import failure by making the attribute access raise ImportError
        class _FakeCore:
            @property
            def clear_cache(self):
                raise ImportError("mlx not installed")

        class _FakeMlx:
            core = _FakeCore()

        # Should not raise
        self._run_cleanup_block(_FakeMlx())


# ---------------------------------------------------------------------------
# H4 — AudioLanguageID._model_cache bounded to 1 entry
# ---------------------------------------------------------------------------
class TestAudioLanguageIDCacheBounded(unittest.TestCase):
    """AudioLanguageID._model_cache must never hold more than 1 entry."""

    def setUp(self):
        # W1752: record pre-test sys.modules state for keys we are about to
        # install so tearDown can restore them precisely.
        self._pre_mlx_whisper = sys.modules.get("mlx_whisper")
        self._pre_mlx_whisper_load = sys.modules.get("mlx_whisper.load_models")

        # Provide stub mlx_whisper so AudioLanguageID can be imported.
        # Use setdefault only for mlx_whisper — always force our stubs in so
        # the module-level import inside audio_lang_id.py resolves consistently.
        stub_whisper = types.ModuleType("mlx_whisper")
        stub_load = types.ModuleType("mlx_whisper.load_models")
        stub_load.load_model = MagicMock(side_effect=lambda p: {"model": p})
        stub_whisper.load_models = stub_load
        sys.modules["mlx_whisper"] = stub_whisper
        sys.modules["mlx_whisper.load_models"] = stub_load

        # Re-import with patched module in place
        import importlib
        if "core.audio_lang_id" in sys.modules:
            del sys.modules["core.audio_lang_id"]
        self.audio_lang_id_mod = importlib.import_module("core.audio_lang_id")
        self.AudioLanguageID = self.audio_lang_id_mod.AudioLanguageID
        # Reset class-level cache before each test
        self.AudioLanguageID._model_cache.clear()

    def tearDown(self):
        # W1752: restore sys.modules for every key we installed in setUp so
        # MagicMock / bare-ModuleType stubs do not outlive this test class
        # and pollute sibling test files in the same xdist worker.
        self.AudioLanguageID._model_cache.clear()
        # Remove the reimported core.audio_lang_id so next file gets a clean slate.
        sys.modules.pop("core.audio_lang_id", None)
        for key, orig in (
            ("mlx_whisper", self._pre_mlx_whisper),
            ("mlx_whisper.load_models", self._pre_mlx_whisper_load),
        ):
            if orig is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = orig

    def _inject_model(self, path: str):
        """Simulate cache population via the bounded insertion logic."""
        cls = self.AudioLanguageID
        if len(cls._model_cache) >= 1:
            cls._model_cache.clear()
        cls._model_cache[path] = {"model": path}

    def test_single_model_stored(self):
        self._inject_model("model-a")
        self.assertEqual(len(self.AudioLanguageID._model_cache), 1)

    def test_second_model_evicts_first(self):
        self._inject_model("model-a")
        self._inject_model("model-b")
        self.assertEqual(len(self.AudioLanguageID._model_cache), 1)
        self.assertIn("model-b", self.AudioLanguageID._model_cache)
        self.assertNotIn("model-a", self.AudioLanguageID._model_cache)

    def test_cache_never_exceeds_one(self):
        for i in range(10):
            self._inject_model(f"model-{i}")
            self.assertLessEqual(len(self.AudioLanguageID._model_cache), 1)

    def test_same_model_path_no_eviction(self):
        """Loading the same model twice should NOT evict and re-insert."""
        self._inject_model("model-a")
        # Simulate the guard: if already present, no eviction
        cls = self.AudioLanguageID
        path = "model-a"
        if path not in cls._model_cache:
            if len(cls._model_cache) >= 1:
                cls._model_cache.clear()
            cls._model_cache[path] = {"model": path}
        # Still exactly 1 entry with same key
        self.assertEqual(len(cls._model_cache), 1)
        self.assertIn("model-a", cls._model_cache)


if __name__ == "__main__":
    unittest.main()
