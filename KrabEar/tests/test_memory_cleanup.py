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
# H2 — mx.clear_cache вызывается НАСТОЯЩИМ кодом очистки (AudioLanguageID.clear_model_cache)
# ---------------------------------------------------------------------------
class TestMxClearCacheAfterTranscribe(unittest.TestCase):
    """mx.clear_cache() зовётся production-методом очистки, а не копией блока в тесте
    (раньше тест гонял собственную копию — удаление очистки из кода его не краснило)."""

    def _clear_with(self, mlx, has_mlx=True):
        from unittest.mock import patch
        import core.audio_lang_id as mod

        with patch.dict(sys.modules, {"mlx": mlx, "mlx.core": mlx.core}), \
                patch.object(mod, "_HAS_MLX", has_mlx):
            mod.AudioLanguageID._model_cache["stale-model"] = object()
            try:
                mod.AudioLanguageID.clear_model_cache()
            finally:
                mod.AudioLanguageID._model_cache.clear()

    def test_clear_cache_called_when_mlx_available(self):
        mlx, core = _make_mlx_stub(has_clear_cache=True)
        self._clear_with(mlx)
        core.clear_cache.assert_called_once()

    def test_clear_cache_not_called_without_mlx(self):
        mlx, core = _make_mlx_stub(has_clear_cache=True)
        self._clear_with(mlx, has_mlx=False)
        core.clear_cache.assert_not_called()

    def test_clear_cache_evicts_cached_model(self):
        import core.audio_lang_id as mod
        mlx, _core = _make_mlx_stub()
        from unittest.mock import patch
        with patch.dict(sys.modules, {"mlx": mlx, "mlx.core": mlx.core}), \
                patch.object(mod, "_HAS_MLX", True):
            mod.AudioLanguageID._model_cache["stale-model"] = object()
            mod.AudioLanguageID.clear_model_cache()
            self.assertEqual(mod.AudioLanguageID._model_cache, {})

    def test_attribute_error_swallowed(self):
        """Старый MLX без clear_cache: исключение подавляется (деградация, не падение)."""
        mlx, core = _make_mlx_stub(has_clear_cache=False)
        self._clear_with(mlx)  # не должно бросить

    def test_import_error_swallowed(self):
        """clear_cache бросает ImportError: подавляется, кэш модели всё равно очищен."""
        import core.audio_lang_id as mod
        mlx, core = _make_mlx_stub()
        core.clear_cache = MagicMock(side_effect=ImportError("mlx not installed"))
        self._clear_with(mlx)
        self.assertEqual(mod.AudioLanguageID._model_cache, {})


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
        self._pre_audio_lang_id = sys.modules.get("core.audio_lang_id")
        # Откат регистрируется ДО первой подмены: ошибка в setUp иначе оставляла бы
        # заглушки в sys.modules (tearDown при сбое setUp не вызывается).
        self.addCleanup(self._restore_modules)

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

    def _restore_modules(self):
        """Возвращает ВСЕ затронутые записи sys.modules, включая прежний core.audio_lang_id."""
        for key, orig in (
            ("mlx_whisper", self._pre_mlx_whisper),
            ("mlx_whisper.load_models", self._pre_mlx_whisper_load),
            ("core.audio_lang_id", self._pre_audio_lang_id),
        ):
            if orig is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = orig

    def tearDown(self):
        # W1752: кэш очищается здесь, модули возвращает addCleanup (_restore_modules).
        cls = getattr(self, "AudioLanguageID", None)
        if cls is not None:
            cls._model_cache.clear()

    def _whisper_double(self):
        """Двойник mlx_whisper, который понимает настоящий _detect_with_mlx (загрузка + LID)."""
        import numpy as np

        whisper = MagicMock()
        whisper.load_models.load_model = MagicMock(side_effect=lambda p: {"model": p})
        whisper.audio.log_mel_spectrogram = MagicMock(return_value=np.zeros((1, 1)))
        whisper.decoding.detect_language = MagicMock(return_value=("ru", {"ru": 0.99}))
        return whisper

    def _detect(self, whisper, path: str):
        """Настоящий AudioLanguageID._detect_with_mlx с моделью по пути path."""
        import numpy as np

        lid = self.AudioLanguageID(model_path=path)
        return lid._detect_with_mlx(whisper, np.ones(16000, dtype=np.float32) * 0.1)

    def test_single_model_stored(self):
        whisper = self._whisper_double()
        self.assertEqual(self._detect(whisper, "model-a"), "ru")
        self.assertEqual(list(self.AudioLanguageID._model_cache), ["model-a"])

    def test_second_model_evicts_first(self):
        whisper = self._whisper_double()
        self._detect(whisper, "model-a")
        self._detect(whisper, "model-b")
        self.assertEqual(list(self.AudioLanguageID._model_cache), ["model-b"])

    def test_cache_never_exceeds_one(self):
        whisper = self._whisper_double()
        for i in range(10):
            self._detect(whisper, f"model-{i}")
            self.assertLessEqual(len(self.AudioLanguageID._model_cache), 1)
        self.assertEqual(whisper.load_models.load_model.call_count, 10)

    def test_same_model_path_no_eviction_no_reload(self):
        """Тот же путь дважды: модель НЕ перегружается и не вытесняется."""
        whisper = self._whisper_double()
        self._detect(whisper, "model-a")
        first = self.AudioLanguageID._model_cache["model-a"]
        self._detect(whisper, "model-a")
        whisper.load_models.load_model.assert_called_once_with("model-a")
        self.assertIs(self.AudioLanguageID._model_cache["model-a"], first)
        self.assertEqual(len(self.AudioLanguageID._model_cache), 1)


if __name__ == "__main__":
    unittest.main()
