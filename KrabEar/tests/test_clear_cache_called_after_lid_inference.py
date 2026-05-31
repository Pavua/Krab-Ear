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
7. test_clear_cache_not_called_on_cache_hit — cache hit → no inference → no clear_cache

W1757 xdist fix: this file was missed by PR #1560 (wave1752) which fixed 4 sibling
MLX-stub-family files.  The CI failure mode: an earlier file in the same sequential
chunk leaves sys.modules["mlx"] / ["mlx.core"] as MagicMock (or poisons core.mlx_lock
module state).  _run_detect (audio_lang_id.py:320) calls
  with mlx_inter_process_lock(), mlx_lock():
When either lock callable raises, the broad except-Exception at line 325 catches it and
returns None BEFORE reaching _detect_with_mlx → clear_cache never called → assertion
fails.  Fix: every detect() call controls the FULL MLX surface — sys.modules["mlx"] +
["mlx.core"], lock context managers, and the mlx_whisper stub — via patch.object on the
live module object, making each test immune to any leaked predecessor state.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Module-level: resolve the live core.audio_lang_id module object ONCE.
# All patches go through patch.object(self._m, ...) so the class-under-test
# and the patched module globals are guaranteed to be the same object, even if
# a sibling file ran importlib.reload() and left a stale top-level binding.
# ---------------------------------------------------------------------------
import core.audio_lang_id as _audio_lang_id_mod  # noqa: E402


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


def _nullcontext_factory():
    """Return a callable that returns a no-op context manager."""
    def _factory(*args, **kwargs):
        return contextlib.nullcontext()
    return _factory


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestClearCacheCalledAfterLIDInference(unittest.TestCase):
    """mx.clear_cache() is called after LID inference (W63 rule, W1358 F2)."""

    def setUp(self):
        # W1757: re-resolve the live module object each setUp so any sibling
        # reload() cannot leave us holding a stale class binding.
        self._m = importlib.import_module("core.audio_lang_id")
        self._AudioLanguageID = self._m.AudioLanguageID
        self._AudioLanguageID._model_cache.clear()

    def _detect_with_mock_mx(self, mock_mlx, mock_mx_module=None):
        """Run detect() with FULL MLX surface controlled.

        W1757: patches sys.modules["mlx"] + ["mlx.core"] to the same mock_mx_module
        object used for core.audio_lang_id.mx, and neutralises both lock context
        managers so leaked lock-module state from predecessor tests cannot prevent
        _detect_with_mlx (and its finally clear_cache) from being reached.

        Returns (result, mock_mx_module).
        """
        if mock_mx_module is None:
            mock_mx_module = MagicMock()
            mock_mx_module.clear_cache = MagicMock()

        lid = self._AudioLanguageID()

        with patch.dict(
            "sys.modules",
            {
                "mlx_whisper": mock_mlx,
                "mlx": mock_mx_module,        # W1757: anchor mlx stub
                "mlx.core": mock_mx_module,   # W1757: same object as mx patch below
            },
        ):
            with patch.object(self._m, "_HAS_MLX", True):
                with patch.object(self._m, "mx", mock_mx_module):
                    # W1757: neutralise lock CMs — leaked lock-module state can
                    # make mlx_lock()/mlx_inter_process_lock() raise, which the
                    # broad except-Exception in _run_detect catches before
                    # _detect_with_mlx is entered, preventing clear_cache.
                    with patch.object(
                        self._m, "mlx_lock", _nullcontext_factory()
                    ):
                        with patch.object(
                            self._m, "mlx_inter_process_lock", _nullcontext_factory()
                        ):
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

        lid = self._AudioLanguageID()
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            with patch.object(self._m, "_HAS_MLX", False):
                with patch.object(self._m, "mx", mock_mx):
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
        lid = self._AudioLanguageID()

        n_calls = 3
        for _ in range(n_calls):
            self._AudioLanguageID._model_cache.clear()
            mock_mlx = _make_mlx_mock("ru")
            with patch.dict(
                "sys.modules",
                {
                    "mlx_whisper": mock_mlx,
                    "mlx": mock_mx,
                    "mlx.core": mock_mx,
                },
            ):
                with patch.object(self._m, "_HAS_MLX", True):
                    with patch.object(self._m, "mx", mock_mx):
                        with patch.object(
                            self._m, "mlx_lock", _nullcontext_factory()
                        ):
                            with patch.object(
                                self._m, "mlx_inter_process_lock", _nullcontext_factory()
                            ):
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

        lid = self._AudioLanguageID()
        # Pre-populate the external cache
        ext_cache = {"audio_lang": "ru"}

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            with patch.object(self._m, "_HAS_MLX", True):
                with patch.object(self._m, "mx", mock_mx):
                    result = lid.detect(_speech(), sample_rate=16000, cache=ext_cache)

        self.assertEqual(result, "ru")
        mock_mx.clear_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
