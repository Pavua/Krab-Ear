"""W1465 — audio_lang_id._run_detect must NOT call mx.clear_cache() outside mlx_lock.

W1462 F1 HIGH: double mx.clear_cache() defect.
- W1117 added clear_cache in _run_detect.finally (OUTSIDE mlx_lock) — WRONG.
- W1367 added clear_cache in _detect_with_mlx.finally (INSIDE mlx_lock) — CORRECT.

Fix: remove the outer _run_detect.finally call. Only the inner one under mlx_lock remains.

Tests:
1. test_mx_clear_cache_called_once_per_inference — single call per detect(), not two
2. test_mx_clear_cache_under_mlx_lock — clear_cache only called while lock is held
3. test_no_outer_finally_clear_cache_in_run_detect — AST check: no clear_cache in
   _run_detect body outside the mlx_lock call
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap
import threading
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID  # noqa: E402


def _fresh_aid_module():
    """Return a fresh core.audio_lang_id module (re-importing if needed).

    Some tests (test_audio_lang_id_cache_limit) delete sys.modules["core.audio_lang_id"]
    and re-import it, leaving two module objects. We always work with the currently
    registered module to avoid patching a stale reference.
    """
    import importlib
    import sys
    # Ensure we have a live module registered (re-import if deleted by another test)
    if "core.audio_lang_id" not in sys.modules:
        importlib.import_module("core.audio_lang_id")
    return sys.modules["core.audio_lang_id"]


class TestMxClearCacheCalledOncePerInference(unittest.TestCase):
    """mx.clear_cache() must be called exactly once per inference, not twice."""

    def setUp(self):
        self.mod = _fresh_aid_module()
        self.mod.AudioLanguageID._model_cache.clear()

    def tearDown(self):
        self.mod.AudioLanguageID._model_cache.clear()

    def test_mx_clear_cache_called_once_per_inference(self):
        """detect() must trigger exactly one mx.clear_cache() call, not two."""
        clear_cache_calls = []
        mod = self.mod

        # Track every clear_cache call via the live module's mx reference
        mock_mx = MagicMock()
        mock_mx.clear_cache.side_effect = lambda: clear_cache_calls.append(1)

        # Build a minimal fake mlx_whisper that completes successfully
        fake_model = MagicMock()
        mock_mlx_whisper = MagicMock()
        mock_mlx_whisper.load_models.load_model.return_value = fake_model
        mock_mlx_whisper.audio.log_mel_spectrogram.return_value = MagicMock()
        mock_mlx_whisper.decoding.detect_language.return_value = "ru"

        import numpy as np

        audio = np.zeros(48000, dtype=np.float32)

        mod_name = mod.__name__  # "core.audio_lang_id"
        with patch(f"{mod_name}._HAS_MLX", True), \
             patch(f"{mod_name}.mx", mock_mx), \
             patch.dict("sys.modules", {"mlx_whisper": mock_mlx_whisper}):
            lid = mod.AudioLanguageID(model_path="fake-model", preview_sec=3.0)
            result = lid.detect(audio, sample_rate=16000)

        self.assertEqual(result, "ru")
        self.assertEqual(
            len(clear_cache_calls), 1,
            f"Expected exactly 1 mx.clear_cache() call, got {len(clear_cache_calls)}. "
            "Double call means _run_detect.finally was NOT removed (W1462 regression)."
        )


class TestMxClearCacheUnderMlxLock(unittest.TestCase):
    """mx.clear_cache() must only be called while mlx_lock() is held."""

    def setUp(self):
        self.mod = _fresh_aid_module()
        self.mod.AudioLanguageID._model_cache.clear()

    def tearDown(self):
        self.mod.AudioLanguageID._model_cache.clear()

    def test_mx_clear_cache_under_mlx_lock(self):
        """clear_cache() must not be called after mlx_lock() context exits."""
        mod = self.mod
        lock_held_during_clear = []

        import core.mlx_lock as mlx_lock_mod
        rlock = mlx_lock_mod._mlx_lock

        mock_mx = MagicMock()

        def tracking_clear_cache():
            # RLock._is_owned() returns True when the current thread holds it.
            is_locked = rlock._is_owned() if hasattr(rlock, "_is_owned") else True
            lock_held_during_clear.append(is_locked)

        mock_mx.clear_cache.side_effect = tracking_clear_cache

        fake_model = MagicMock()
        mock_mlx_whisper = MagicMock()
        mock_mlx_whisper.load_models.load_model.return_value = fake_model
        mock_mlx_whisper.audio.log_mel_spectrogram.return_value = MagicMock()
        mock_mlx_whisper.decoding.detect_language.return_value = "en"

        import numpy as np
        audio = np.zeros(32000, dtype=np.float32)

        mod_name = mod.__name__
        with patch(f"{mod_name}._HAS_MLX", True), \
             patch(f"{mod_name}.mx", mock_mx), \
             patch.dict("sys.modules", {"mlx_whisper": mock_mlx_whisper}):
            lid = mod.AudioLanguageID(model_path="test-model", preview_sec=2.0)
            lid.detect(audio, sample_rate=16000)

        # clear_cache must have been called at least once
        self.assertGreater(
            len(lock_held_during_clear), 0,
            "mx.clear_cache() was never called — test setup may be wrong"
        )
        # All clear_cache calls must have occurred while the lock was held
        for i, held in enumerate(lock_held_during_clear):
            self.assertTrue(
                held,
                f"clear_cache() call #{i+1} occurred outside mlx_lock() — "
                "this violates MLX thread-safety policy (W1462 regression)"
            )


class TestNoOuterFinallyClearCacheInRunDetect(unittest.TestCase):
    """AST-level check: _run_detect must have no finally block with clear_cache."""

    def test_no_outer_finally_clear_cache_in_run_detect(self):
        """_run_detect source must NOT contain a finally block that calls clear_cache."""
        import core.audio_lang_id as module
        source = textwrap.dedent(inspect.getsource(module.AudioLanguageID._run_detect))
        tree = ast.parse(source)

        # Walk all Try nodes in _run_detect and check their finalbody
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not node.finalbody:
                continue
            # Flatten the finally body to find any clear_cache calls
            for stmt in ast.walk(ast.Module(body=node.finalbody, type_ignores=[])):
                if isinstance(stmt, ast.Call):
                    # Check for attribute calls: _mx.clear_cache() or mx.clear_cache()
                    if isinstance(stmt.func, ast.Attribute):
                        if stmt.func.attr == "clear_cache":
                            self.fail(
                                "_run_detect contains a finally block with "
                                "clear_cache() call — this is the W1462 defect. "
                                "The outer finally must be removed; clear_cache "
                                "belongs only in _detect_with_mlx.finally (under mlx_lock)."
                            )

    def test_detect_with_mlx_has_finally_with_clear_cache(self):
        """Sanity: _detect_with_mlx must still have the correct finally+clear_cache."""
        import core.audio_lang_id as module
        source = textwrap.dedent(inspect.getsource(module.AudioLanguageID._detect_with_mlx))
        tree = ast.parse(source)

        found_finally_clear = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not node.finalbody:
                continue
            for stmt in ast.walk(ast.Module(body=node.finalbody, type_ignores=[])):
                if isinstance(stmt, ast.Call):
                    if isinstance(stmt.func, ast.Attribute):
                        if stmt.func.attr == "clear_cache":
                            found_finally_clear = True

        self.assertTrue(
            found_finally_clear,
            "_detect_with_mlx must have a finally block with clear_cache() — "
            "that is the correct (under-mlx_lock) call site (W1367 addition)."
        )


if __name__ == "__main__":
    unittest.main()
