"""W1368 regression test: WhisperMLXAdapter.transcribe() must call mx.clear_cache()
after mlx_whisper.transcribe() — W63 rule gap fix.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest \
        KrabEar/tests/test_whisper_mlx_adapter_clear_cache_W1368.py -v

W1752 xdist fix: importlib.reload() inside patch.dict() leaves the reloaded
module cached in sys.modules under a state that was valid only while the
patch.dict context was active.  After context exit the patch is unwound but
sys.modules["core.pipeline.stt_whisper_mlx_adapter"] still points to the
reloaded copy, whose module-level globals reference the now-removed fake stubs.
tearDown removes the reloaded entry so the next test (or file) gets a fresh
import with a consistent sys.modules state.
"""

from __future__ import annotations

import ast
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_audio(seconds: float = 0.5, sample_rate: int = 16000) -> np.ndarray:
    """Generate synthetic float32 PCM audio."""
    t = np.linspace(0, seconds, int(seconds * sample_rate), dtype=np.float32)
    return np.sin(2 * np.pi, 440 * t)


def _fake_mlx_whisper_module(result_text: str = "Привет") -> types.ModuleType:
    """Return a fake mlx_whisper module that records calls."""
    mod = types.ModuleType("mlx_whisper")
    mock_transcribe = MagicMock(return_value={"text": result_text, "language": "ru"})
    mod.transcribe = mock_transcribe
    return mod


def _fake_mlx_core_module() -> types.ModuleType:
    """Return a fake mlx.core module with a tracked clear_cache."""
    mod = types.ModuleType("mlx.core")
    mod.clear_cache = MagicMock()
    return mod


class TestClearCacheCalledAfterWhisperMLXAdapterTranscribe(unittest.TestCase):
    """mx.clear_cache() must be called inside mlx_lock() block after transcribe."""

    # W1752: keys installed into sys.modules via importlib.reload() inside
    # patch.dict() — we evict them in tearDown to prevent state leaks to
    # sibling test files in the same xdist worker.
    _RELOAD_MODULE_KEY = "core.pipeline.stt_whisper_mlx_adapter"

    def setUp(self):
        # Record pre-test sys.modules state for the adapter module.
        self._pre_adapter_mod = sys.modules.get(self._RELOAD_MODULE_KEY)

        # Build fake mlx package hierarchy: mlx + mlx.core
        self.fake_mlx_core = _fake_mlx_core_module()
        self.fake_mlx = types.ModuleType("mlx")
        self.fake_mlx.core = self.fake_mlx_core

        self.fake_mlx_whisper = _fake_mlx_whisper_module()

    def tearDown(self):
        # W1752: restore (or evict) the adapter module that was reload()ed
        # inside the patch.dict() context.  After the context exits the
        # module's globals still reference the now-unwound fake stubs.
        # Removing it forces a fresh import next time, preventing cross-file
        # state pollution in xdist workers.
        if self._pre_adapter_mod is None:
            sys.modules.pop(self._RELOAD_MODULE_KEY, None)
        else:
            sys.modules[self._RELOAD_MODULE_KEY] = self._pre_adapter_mod

    def _patch_imports(self):
        """Context manager that patches sys.modules for mlx and mlx_whisper."""
        return patch.dict(
            "sys.modules",
            {
                "mlx": self.fake_mlx,
                "mlx.core": self.fake_mlx_core,
                "mlx_whisper": self.fake_mlx_whisper,
            },
        )

    def test_clear_cache_called_after_whisper_mlx_adapter_transcribe(self):
        """mx.clear_cache() is called once per transcribe() invocation."""
        audio = np.zeros(8000, dtype=np.float32)

        with self._patch_imports():
            # Reload the adapter module so it picks up the patched sys.modules
            import importlib
            import core.pipeline.stt_whisper_mlx_adapter as _mod
            importlib.reload(_mod)
            adapter = _mod.WhisperMLXAdapter()

            with patch("core.mlx_lock.mlx_lock") as mock_lock:
                # Use a real nullcontext so the body still executes
                import contextlib
                mock_lock.return_value = contextlib.nullcontext()

                adapter.transcribe(audio, language="ru")

            # clear_cache should have been called
            self.fake_mlx_core.clear_cache.assert_called_once()

    def test_clear_cache_called_on_first_variant_success(self):
        """clear_cache is still called when the first param variant succeeds."""
        audio = np.zeros(8000, dtype=np.float32)

        with self._patch_imports():
            import importlib
            import core.pipeline.stt_whisper_mlx_adapter as _mod
            importlib.reload(_mod)
            adapter = _mod.WhisperMLXAdapter()

            with patch("core.mlx_lock.mlx_lock") as mock_lock:
                import contextlib
                mock_lock.return_value = contextlib.nullcontext()

                adapter.transcribe(audio)

            self.fake_mlx_core.clear_cache.assert_called_once()
            # mlx_whisper.transcribe was called exactly once (first variant hit)
            self.assertEqual(self.fake_mlx_whisper.transcribe.call_count, 1)

    def test_clear_cache_called_even_after_fallback_variants(self):
        """clear_cache is called even when earlier param variants raise TypeError."""
        audio = np.zeros(8000, dtype=np.float32)

        # Make first two variants fail with TypeError; third succeeds.
        call_count = {"n": 0}
        original_result = {"text": "test", "language": "en"}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise TypeError("unexpected kwarg")
            return original_result

        self.fake_mlx_whisper.transcribe.side_effect = side_effect

        with self._patch_imports():
            import importlib
            import core.pipeline.stt_whisper_mlx_adapter as _mod
            importlib.reload(_mod)
            adapter = _mod.WhisperMLXAdapter()

            with patch("core.mlx_lock.mlx_lock") as mock_lock:
                import contextlib
                mock_lock.return_value = contextlib.nullcontext()

                adapter.transcribe(audio)

            # Exactly 3 calls (2 TypeError + 1 success)
            self.assertEqual(call_count["n"], 3)
            # clear_cache still called once
            self.fake_mlx_core.clear_cache.assert_called_once()

    def test_clear_cache_swallows_import_error(self):
        """If mlx.core is not importable, transcribe() still returns a result."""
        audio = np.zeros(8000, dtype=np.float32)

        # Patch sys.modules to remove mlx.core so ImportError is raised
        modules_no_mlx = {
            "mlx_whisper": self.fake_mlx_whisper,
            # mlx and mlx.core intentionally absent
        }
        with patch.dict("sys.modules", modules_no_mlx, clear=False):
            # Also block mlx imports
            with patch.dict("sys.modules", {"mlx": None, "mlx.core": None}):
                import importlib
                import core.pipeline.stt_whisper_mlx_adapter as _mod
                importlib.reload(_mod)
                adapter = _mod.WhisperMLXAdapter()

                with patch("core.mlx_lock.mlx_lock") as mock_lock:
                    import contextlib
                    mock_lock.return_value = contextlib.nullcontext()

                    # Should not raise even when clear_cache can't be called
                    result = adapter.transcribe(audio)

                self.assertEqual(result.text, "Привет")


class TestWhisperMLXAdapterClearCacheAST(unittest.TestCase):
    """AST-level check: mx.clear_cache() call appears inside mlx_lock() with-block."""

    _ADAPTER_PATH = os.path.join(
        os.path.dirname(__file__),
        "..",
        "core",
        "pipeline",
        "stt_whisper_mlx_adapter.py",
    )

    def _load_ast(self) -> ast.Module:
        with open(self._ADAPTER_PATH, encoding="utf-8") as f:
            return ast.parse(f.read())

    def test_clear_cache_call_present_in_source(self):
        """Source contains mx.clear_cache() call."""
        tree = self._load_ast()
        found = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear_cache"
            ):
                found = True
                break
        self.assertTrue(found, "mx.clear_cache() call not found in adapter source")

    def test_clear_cache_inside_mlx_lock_with_block(self):
        """mx.clear_cache() is inside the with mlx_lock() block, not after it."""
        tree = self._load_ast()
        transcribe_func: ast.FunctionDef | None = None

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "transcribe":
                transcribe_func = node
                break

        self.assertIsNotNone(transcribe_func, "transcribe() method not found")

        # Find the with mlx_lock() statement
        with_node: ast.With | None = None
        for node in ast.walk(transcribe_func):  # type: ignore[arg-type]
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                ctx = item.context_expr
                # Match: with mlx_lock(): ...
                if (
                    isinstance(ctx, ast.Call)
                    and isinstance(ctx.func, ast.Name)
                    and ctx.func.id == "mlx_lock"
                ):
                    with_node = node
                    break
            if with_node:
                break

        self.assertIsNotNone(with_node, "with mlx_lock() block not found in transcribe()")

        # Verify clear_cache() call is inside the with block
        clear_cache_found = False
        for node in ast.walk(with_node):  # type: ignore[arg-type]
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear_cache"
            ):
                clear_cache_found = True
                break

        self.assertTrue(
            clear_cache_found,
            "mx.clear_cache() call is not inside the with mlx_lock() block",
        )

    def test_clear_cache_inside_try_except(self):
        """mx.clear_cache() is wrapped in try/except to handle missing mlx gracefully."""
        tree = self._load_ast()
        transcribe_func: ast.FunctionDef | None = None

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "transcribe":
                transcribe_func = node
                break

        self.assertIsNotNone(transcribe_func)

        # Find try/except that contains clear_cache
        found_try_except = False
        for node in ast.walk(transcribe_func):  # type: ignore[arg-type]
            if not isinstance(node, ast.Try):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "clear_cache"
                ):
                    found_try_except = True
                    break
            if found_try_except:
                break

        self.assertTrue(
            found_try_except,
            "mx.clear_cache() is not wrapped in try/except in transcribe()",
        )


if __name__ == "__main__":
    unittest.main()
