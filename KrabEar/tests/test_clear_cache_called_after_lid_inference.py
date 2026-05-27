"""W1367 — test_clear_cache_called_after_lid_inference.

Verifies that mx.clear_cache() is called after every LID inference cycle
inside the mlx_lock() context, as required by the W63 rule (CLAUDE.md).

Test cases:
1. test_clear_cache_called_on_successful_inference — successful detect → mx.clear_cache() called
2. test_clear_cache_called_on_detect_language_exception — detect_language raises → clear_cache still called (finally)
3. test_clear_cache_called_on_mel_spectrogram_exception — log_mel raises → clear_cache still called (finally)
4. test_clear_cache_not_called_when_mlx_unavailable — _HAS_MLX=False → clear_cache NOT called
5. test_clear_cache_called_once_per_inference — single detect() → exactly 1 clear_cache call
6. test_clear_cache_called_on_each_inference — N detect() calls → N clear_cache calls
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _speech(seconds: float = 3.0, sr: int = 16000) -> np.ndarray:
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    return (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _make_mlx_mock(lang: str = "ru") -> MagicMock:
    mock_mlx = MagicMock()
    mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
    mock_mlx.load_models.load_model.return_value = MagicMock()
    mock_mlx.decoding.detect_language.return_value = (lang, {lang: 0.9})
    return mock_mlx


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestClearCacheCalledAfterLIDInference(unittest.TestCase):
    """mx.clear_cache() is called after LID inference (W63 rule, W1358 F2)."""

    def setUp(self):
        AudioLanguageID._model_cache.clear()

    def _detect_with_mock_mx(self, mock_mlx, mock_mx_module=None, extra_patches=None):
        """Run detect() with mocked mlx_whisper and optional mlx.core mock.

        Returns (result, mock_mx) where mock_mx has a clear_cache attribute.
        """
        if mock_mx_module is None:
            mock_mx_module = MagicMock()
            mock_mx_module.clear_cache = MagicMock()

        patches = {
            "sys.modules": {"mlx_whisper": mock_mlx},
        }
        if extra_patches:
            patches.update(extra_patches)

        lid = AudioLanguageID()
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            with patch("core.audio_lang_id._HAS_MLX", True):
                with patch("core.audio_lang_id.mx", mock_mx_module):
                    result = lid.detect(_speech(), sample_rate=16000)
        return result, mock_mx_module

    # ------------------------------------------------------------------
    # 1. Successful inference → clear_cache called
    # ------------------------------------------------------------------

    def test_clear_cache_called_on_successful_inference(self):
        """mx.clear_cache() called once after successful LID detect_language call."""
        mock_mlx = _make_mlx_mock("ru")
        result, mock_mx = self._detect_with_mock_mx(mock_mlx)

        self.assertEqual(result, "ru")
        mock_mx.clear_cache.assert_called_once()

    # ------------------------------------------------------------------
    # 2. detect_language raises → clear_cache still called via finally
    # ------------------------------------------------------------------

    def test_clear_cache_called_on_detect_language_exception(self):
        """mx.clear_cache() called even when detect_language raises (finally block)."""
        mock_mlx = _make_mlx_mock()
        mock_mlx.decoding.detect_language.side_effect = RuntimeError("GPU hang")

        mock_mx = MagicMock()
        result, mock_mx = self._detect_with_mock_mx(mock_mlx, mock_mx)

        # detect() returns None on exception
        self.assertIsNone(result)
        # clear_cache must still have been called
        mock_mx.clear_cache.assert_called_once()

    # ------------------------------------------------------------------
    # 3. log_mel_spectrogram raises → clear_cache still called via finally
    # ------------------------------------------------------------------

    def test_clear_cache_called_on_mel_spectrogram_exception(self):
        """mx.clear_cache() called even when log_mel_spectrogram raises."""
        mock_mlx = _make_mlx_mock()
        mock_mlx.audio.log_mel_spectrogram.side_effect = RuntimeError("Metal OOM")

        mock_mx = MagicMock()
        result, mock_mx = self._detect_with_mock_mx(mock_mlx, mock_mx)

        self.assertIsNone(result)
        mock_mx.clear_cache.assert_called_once()

    # ------------------------------------------------------------------
    # 4. _HAS_MLX=False → clear_cache NOT called (mx unavailable)
    # ------------------------------------------------------------------

    def test_clear_cache_not_called_when_mlx_unavailable(self):
        """When _HAS_MLX=False (mlx.core not installed), clear_cache is NOT called."""
        mock_mlx = _make_mlx_mock("en")
        mock_mx = MagicMock()

        lid = AudioLanguageID()
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            with patch("core.audio_lang_id._HAS_MLX", False):
                with patch("core.audio_lang_id.mx", mock_mx):
                    result = lid.detect(_speech(), sample_rate=16000)

        mock_mx.clear_cache.assert_not_called()

    # ------------------------------------------------------------------
    # 5. Exactly 1 clear_cache call per single detect()
    # ------------------------------------------------------------------

    def test_clear_cache_called_once_per_inference(self):
        """Exactly one mx.clear_cache() call for one detect() invocation."""
        mock_mlx = _make_mlx_mock("es")
        result, mock_mx = self._detect_with_mock_mx(mock_mlx)

        self.assertEqual(result, "es")
        self.assertEqual(mock_mx.clear_cache.call_count, 1,
                         "Expected exactly 1 clear_cache call per inference")

    # ------------------------------------------------------------------
    # 6. N detect() calls → N clear_cache calls (no caching skips inference)
    # ------------------------------------------------------------------

    def test_clear_cache_called_on_each_inference(self):
        """Each detect() call without external cache triggers one clear_cache call."""
        mock_mx = MagicMock()
        lid = AudioLanguageID()

        n_calls = 3
        for _ in range(n_calls):
            AudioLanguageID._model_cache.clear()
            mock_mlx = _make_mlx_mock("ru")
            with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
                with patch("core.audio_lang_id._HAS_MLX", True):
                    with patch("core.audio_lang_id.mx", mock_mx):
                        lid.detect(_speech(), sample_rate=16000)

        self.assertEqual(mock_mx.clear_cache.call_count, n_calls,
                         f"Expected {n_calls} clear_cache calls for {n_calls} inferences")

    # ------------------------------------------------------------------
    # 7. Cache hit path skips inference → no clear_cache call
    # ------------------------------------------------------------------

    def test_clear_cache_not_called_on_cache_hit(self):
        """When detect() returns from external cache, no inference → no clear_cache."""
        mock_mlx = _make_mlx_mock("ru")
        mock_mx = MagicMock()

        lid = AudioLanguageID()
        # Pre-populate the external cache
        ext_cache = {"audio_lang": "ru"}

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            with patch("core.audio_lang_id._HAS_MLX", True):
                with patch("core.audio_lang_id.mx", mock_mx):
                    result = lid.detect(_speech(), sample_rate=16000, cache=ext_cache)

        self.assertEqual(result, "ru")
        mock_mx.clear_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
