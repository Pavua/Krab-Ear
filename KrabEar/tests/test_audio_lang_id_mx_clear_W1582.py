"""W1582 — audio_lang_id: mx.clear_cache() in _detect_with_mlx.finally + clear_model_cache.

W1575 F2+F3 HIGH: W1497 cherry-pick train silently reverted:
  - _HAS_MLX module-level flag (W1416/W1465 guard)
  - mx.clear_cache() in _detect_with_mlx.finally (W1367/W1416/W1465)
  - mx.clear_cache() in clear_model_cache (W1405/W1416)

This test file verifies the W1582 restore.

Tests:
1. test_detect_with_mlx_calls_clear_cache_in_finally
   - mx.clear_cache() called in _detect_with_mlx.finally (inside mlx_lock)
2. test_clear_model_cache_invokes_mx_clear_cache
   - clear_model_cache() calls mx.clear_cache() when _HAS_MLX is True
3. test_clear_cache_failure_does_not_propagate
   - if mx.clear_cache() raises inside _detect_with_mlx.finally, no exception propagated
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID  # noqa: E402


def _fresh_aid_module():
    """Return live core.audio_lang_id module (re-import if deleted by other tests)."""
    import importlib
    if "core.audio_lang_id" not in sys.modules:
        importlib.import_module("core.audio_lang_id")
    return sys.modules["core.audio_lang_id"]


class TestDetectWithMlxCallsClearCacheInFinally(unittest.TestCase):
    """_detect_with_mlx.finally must call mx.clear_cache() (W1367/W1465/W1582)."""

    def setUp(self):
        self.mod = _fresh_aid_module()
        self.mod.AudioLanguageID._model_cache.clear()

    def tearDown(self):
        self.mod.AudioLanguageID._model_cache.clear()

    def test_detect_with_mlx_calls_clear_cache_in_finally(self):
        """mx.clear_cache() must be called exactly once after a successful LID inference."""
        mod = self.mod
        clear_calls = []
        mock_mx = MagicMock()
        mock_mx.clear_cache.side_effect = lambda: clear_calls.append(1)

        fake_model = MagicMock()
        mock_mlx_whisper = MagicMock()
        mock_mlx_whisper.load_models.load_model.return_value = fake_model
        mock_mlx_whisper.audio.log_mel_spectrogram.return_value = MagicMock()
        mock_mlx_whisper.decoding.detect_language.return_value = "es"

        audio = np.zeros(48000, dtype=np.float32)
        mod_name = mod.__name__

        with patch(f"{mod_name}._HAS_MLX", True), \
             patch(f"{mod_name}.mx", mock_mx), \
             patch(f"{mod_name}._MIN_PEAK_AMPLITUDE", 0.0), \
             patch.dict("sys.modules", {"mlx_whisper": mock_mlx_whisper}):
            lid = mod.AudioLanguageID(model_path="test-model", preview_sec=3.0)
            result = lid.detect(audio, sample_rate=16000)

        self.assertEqual(result, "es")
        self.assertEqual(
            len(clear_calls), 1,
            f"Expected 1 mx.clear_cache() call from _detect_with_mlx.finally, "
            f"got {len(clear_calls)}. W1582 restore may be incomplete."
        )


class TestClearModelCacheInvokesMxClearCache(unittest.TestCase):
    """clear_model_cache() must call mx.clear_cache() when _HAS_MLX is True (W1416/W1582)."""

    def setUp(self):
        AudioLanguageID._model_cache["some-model"] = MagicMock()

    def tearDown(self):
        AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_invokes_mx_clear_cache(self):
        """When _HAS_MLX is True, clear_model_cache() must call mx.clear_cache()."""
        clear_calls = []

        try:
            import mlx.core as _real_mx
            # MLX is installed — patch the clear_cache attribute on the real module
            with patch("core.audio_lang_id._HAS_MLX", True), \
                 patch.object(_real_mx, "clear_cache", side_effect=lambda: clear_calls.append(1)):
                AudioLanguageID.clear_model_cache()
        except ImportError:
            # MLX not installed — use sys.modules patching
            mock_mx = MagicMock()
            mock_mx.clear_cache.side_effect = lambda: clear_calls.append(1)
            with patch("core.audio_lang_id._HAS_MLX", True), \
                 patch.dict("sys.modules", {"mlx.core": mock_mx}):
                AudioLanguageID.clear_model_cache()

        self.assertEqual(len(AudioLanguageID._model_cache), 0,
                         "Python cache dict должен быть пуст")
        self.assertEqual(
            len(clear_calls), 1,
            f"clear_model_cache() must call mx.clear_cache() once when _HAS_MLX=True, "
            f"got {len(clear_calls)} calls. W1405/W1416 restore may be incomplete."
        )

    def test_clear_model_cache_no_mx_call_when_has_mlx_false(self):
        """When _HAS_MLX is False, clear_model_cache() must NOT call mx.clear_cache()."""
        clear_calls = []

        try:
            import mlx.core as _real_mx
            with patch("core.audio_lang_id._HAS_MLX", False), \
                 patch.object(_real_mx, "clear_cache", side_effect=lambda: clear_calls.append(1)):
                AudioLanguageID.clear_model_cache()
        except ImportError:
            mock_mx = MagicMock()
            mock_mx.clear_cache.side_effect = lambda: clear_calls.append(1)
            with patch("core.audio_lang_id._HAS_MLX", False), \
                 patch.dict("sys.modules", {"mlx.core": mock_mx}):
                AudioLanguageID.clear_model_cache()

        self.assertEqual(len(clear_calls), 0,
                         "When _HAS_MLX is False, clear_model_cache() must NOT call mx.clear_cache()")
        self.assertEqual(len(AudioLanguageID._model_cache), 0)


class TestClearCacheFailureDoesNotPropagate(unittest.TestCase):
    """mx.clear_cache() errors in _detect_with_mlx.finally must not propagate (W1582)."""

    def setUp(self):
        self.mod = _fresh_aid_module()
        self.mod.AudioLanguageID._model_cache.clear()

    def tearDown(self):
        self.mod.AudioLanguageID._model_cache.clear()

    def test_clear_cache_failure_does_not_propagate(self):
        """If mx.clear_cache() raises in _detect_with_mlx.finally, detect() must not raise."""
        mod = self.mod
        mock_mx = MagicMock()
        mock_mx.clear_cache.side_effect = RuntimeError("Metal OOM — simulated")

        fake_model = MagicMock()
        mock_mlx_whisper = MagicMock()
        mock_mlx_whisper.load_models.load_model.return_value = fake_model
        mock_mlx_whisper.audio.log_mel_spectrogram.return_value = MagicMock()
        mock_mlx_whisper.decoding.detect_language.return_value = "ru"

        audio = np.zeros(48000, dtype=np.float32)
        mod_name = mod.__name__

        # detect() must succeed and return the inferred language even if clear_cache fails
        with patch(f"{mod_name}._HAS_MLX", True), \
             patch(f"{mod_name}.mx", mock_mx), \
             patch(f"{mod_name}._MIN_PEAK_AMPLITUDE", 0.0), \
             patch.dict("sys.modules", {"mlx_whisper": mock_mlx_whisper}):
            lid = mod.AudioLanguageID(model_path="test-model", preview_sec=3.0)
            try:
                result = lid.detect(audio, sample_rate=16000)
            except Exception as exc:
                self.fail(
                    f"detect() raised {type(exc).__name__}: {exc} — "
                    "mx.clear_cache() exception must be silenced in finally block"
                )

        self.assertEqual(result, "ru",
                         "detect() should return inferred language even when clear_cache raises")

    def test_clear_cache_failure_in_clear_model_cache_does_not_propagate(self):
        """If mx.clear_cache() raises in clear_model_cache(), it must not propagate."""
        AudioLanguageID._model_cache["model"] = MagicMock()

        boom = RuntimeError("Metal error — simulated")

        try:
            import mlx.core as _real_mx
            with patch("core.audio_lang_id._HAS_MLX", True), \
                 patch.object(_real_mx, "clear_cache", side_effect=boom):
                try:
                    AudioLanguageID.clear_model_cache()
                except Exception as exc:
                    self.fail(
                        f"clear_model_cache() raised {type(exc).__name__}: {exc} — "
                        "mx.clear_cache() exceptions must be silenced (W1416/W1582)"
                    )
        except ImportError:
            mock_mx = MagicMock()
            mock_mx.clear_cache.side_effect = boom
            with patch("core.audio_lang_id._HAS_MLX", True), \
                 patch.dict("sys.modules", {"mlx.core": mock_mx}):
                try:
                    AudioLanguageID.clear_model_cache()
                except Exception as exc:
                    self.fail(
                        f"clear_model_cache() raised {type(exc).__name__}: {exc} — "
                        "mx.clear_cache() exceptions must be silenced (W1416/W1582)"
                    )

        self.assertEqual(len(AudioLanguageID._model_cache), 0,
                         "Python cache must be cleared even when mx.clear_cache() raises")


if __name__ == "__main__":
    unittest.main()
