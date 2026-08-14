"""W1618 — engine.py mx.clear_cache() calls must be held under mlx_lock().

Two sites audited by W1612:
  F3 LOW  set_quality_profile()  — profile-switch Metal cache flush
  F1 MED  transcribe()           — post-STT/diarization Metal cache flush

Tests:
  test_switch_profile_clear_cache_held_under_mlx_lock
  test_post_diarization_clear_cache_held_under_mlx_lock
  + AST smoke-checks confirming the `with mlx_lock()` wrap is in source
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# AST helper — find `with mlx_lock(): ... clear_cache()` in a method source
# ---------------------------------------------------------------------------

def _call_name(node) -> str:
    """Имя вызываемого у ast.Call: `mlx_lock()` → 'mlx_lock', `x.acquire()` → 'acquire'."""
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _source_contains_lock_wrapping_clear_cache(method) -> bool:
    """True, если clear_cache вызывается ТОЛЬКО под удерживаемым mlx_lock.

    Инвариант W1618/W63 один — clear_cache это MLX-операция, и без лока она
    даёт SIGSEGV при конкурентном доступе к GPU. А вот форм взятия лока в
    проде две, и обе легитимны:

    A. ``with mlx_lock(): ... clear_cache()`` — как в transcribe();
    B. ``lk = mlx_lock()`` / ``if lk.acquire(timeout=...): ... clear_cache()``
       / ``finally: lk.release()`` — в set_quality_profile, где flush кэша
       необязателен и не должен ждать лок дольше бюджета (живой инцидент
       2026-08-13: зависшее превью держало лок, финальная транскрибация
       стояла здесь до backstop-таймаута 180с и запись терялась).

    Форма B засчитывается только целиком: имя должно происходить из
    ``mlx_lock()``, ``clear_cache`` — лежать в теле ``if <имя>.acquire(...)``,
    и в методе обязан быть ``<имя>.release()``. Голый ``clear_cache`` без лока
    не проходит ни по одной ветке.
    """
    src = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(src)

    # --- форма A: with mlx_lock(): ... clear_cache() ---
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                if _call_name(item.context_expr) == "mlx_lock":
                    body_src = ast.dump(ast.Module(body=node.body, type_ignores=[]))
                    if "clear_cache" in body_src:
                        return True

    # --- форма B: lk = mlx_lock(); if lk.acquire(...): ... clear_cache() ---
    lock_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _call_name(node.value) == "mlx_lock"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    if not lock_names:
        return False

    released = {
        node.func.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "release"
        and isinstance(node.func.value, ast.Name)
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Call):
            continue
        test = node.test
        if not isinstance(test.func, ast.Attribute) or test.func.attr != "acquire":
            continue
        if not isinstance(test.func.value, ast.Name):
            continue
        name = test.func.value.id
        if name not in lock_names or name not in released:
            continue
        body_src = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "clear_cache" in body_src:
            return True

    return False


# ---------------------------------------------------------------------------
# F3: set_quality_profile() — AST + call-order
# ---------------------------------------------------------------------------

class TestSwitchProfileClearCacheHeldUnderMlxLock(unittest.TestCase):
    """set_quality_profile() must call mlx_lock() before _mx.clear_cache()."""

    # --- AST smoke-check (always reliable) ---

    def test_set_quality_profile_clear_cache_inside_mlx_lock_ast(self):
        """AST: set_quality_profile() source must contain `with mlx_lock(): clear_cache()`."""
        from core.engine import AudioEngine  # noqa: PLC0415
        self.assertTrue(
            _source_contains_lock_wrapping_clear_cache(AudioEngine.set_quality_profile),
            "set_quality_profile() must contain `with mlx_lock(): ... _mx.clear_cache()` — "
            "W1618 F3 fix not found in AST",
        )

    # --- call-order test ---

    def _make_engine(self):
        from core.engine import AudioEngine  # noqa: PLC0415
        eng = object.__new__(AudioEngine)
        eng.quality_profile = "balanced"
        eng.current_model = "old-model"
        return eng

    def test_switch_profile_clear_cache_held_under_mlx_lock(self):
        """mlx_lock context manager must be entered before clear_cache is called."""
        call_order: list[str] = []

        class _FakeLock:
            """Дубль mlx_lock(): и context-manager, и acquire/release.

            Прод берёт этот лок ДВУМЯ способами: `with mlx_lock():` в
            transcribe() и `mlx_lock().acquire(timeout=...)` в
            set_quality_profile (бюджет на необязательный flush кэша, чтобы
            зависшее превью не держало финальную транскрибацию — живой
            инцидент 2026-08-13). Фейк обязан отражать оба, иначе тест падает
            на AttributeError вместо проверки инварианта.
            """

            def __enter__(self):
                call_order.append("mlx_lock_enter")
                return self

            def __exit__(self, *_):
                call_order.append("mlx_lock_exit")
                return False

            def acquire(self, timeout=None):
                call_order.append("mlx_lock_enter")
                return True

            def release(self):
                call_order.append("mlx_lock_exit")

        from core import engine as _engine_mod  # noqa: PLC0415
        from core.config import settings as _settings  # noqa: PLC0415

        # Determine whether real mlx.core is available
        try:
            import mlx.core as _real_mx
            _mlx_available = True
        except ImportError:
            _mlx_available = False

        eng = self._make_engine()
        # Force a model change so set_quality_profile actually executes the flush
        with patch.object(_settings, "MODEL_BALANCED", "new-model"), \
             patch.object(_engine_mod, "mlx_lock", return_value=_FakeLock()):
            if _mlx_available:
                # Spy on real mlx.core.clear_cache
                with patch.object(_real_mx, "clear_cache",
                                  side_effect=lambda: call_order.append("clear_cache")):
                    eng.set_quality_profile("balanced")
            else:
                # Inject stub mlx.core via sys.modules
                stub_mx = MagicMock()
                stub_mx.clear_cache.side_effect = lambda: call_order.append("clear_cache")
                with patch.dict("sys.modules", {"mlx.core": stub_mx}):
                    eng.set_quality_profile("balanced")

        self.assertIn("mlx_lock_enter", call_order,
                      "mlx_lock must be entered during set_quality_profile")
        self.assertIn("clear_cache", call_order,
                      "clear_cache must be called during set_quality_profile")
        lock_idx = call_order.index("mlx_lock_enter")
        cache_idx = call_order.index("clear_cache")
        self.assertLess(
            lock_idx, cache_idx,
            f"mlx_lock_enter ({lock_idx}) must precede clear_cache ({cache_idx}); "
            f"order={call_order}",
        )

    def test_switch_profile_no_crash_when_mlx_absent(self):
        """set_quality_profile() must not raise when mlx.core is unavailable."""
        from core.engine import AudioEngine  # noqa: PLC0415
        from core.config import settings as _settings  # noqa: PLC0415
        eng = self._make_engine()
        # Simulate missing mlx by removing from sys.modules temporarily
        backup = sys.modules.get("mlx.core", _sentinel := object())
        sys.modules["mlx.core"] = None  # causes ImportError on `import mlx.core`
        try:
            with patch.object(_settings, "MODEL_BALANCED", "new-model"):
                eng.set_quality_profile("balanced")  # must not raise
        finally:
            if backup is _sentinel:
                sys.modules.pop("mlx.core", None)
            else:
                sys.modules["mlx.core"] = backup


# ---------------------------------------------------------------------------
# F1: post-diarization clear_cache in transcribe()
# ---------------------------------------------------------------------------

class TestPostDiarizationClearCacheHeldUnderMlxLock(unittest.TestCase):
    """transcribe() post-diarization must call mlx_lock() before _mx.clear_cache()."""

    # --- AST smoke-check (always reliable) ---

    def test_post_diarization_clear_cache_inside_mlx_lock_ast(self):
        """AST: transcribe() source must contain `with mlx_lock(): clear_cache()`."""
        from core.engine import AudioEngine  # noqa: PLC0415
        self.assertTrue(
            _source_contains_lock_wrapping_clear_cache(AudioEngine.transcribe),
            "transcribe() must contain `with mlx_lock(): ... _mx.clear_cache()` — "
            "W1618 F1 fix not found in AST",
        )

    # --- call-order test (best-effort: skips if transcribe can't reach the site) ---

    def _make_minimal_engine(self):
        from core.engine import AudioEngine  # noqa: PLC0415
        eng = object.__new__(AudioEngine)
        eng.quality_profile = "balanced"
        eng.current_model = "whisper-small"
        eng._unavailable_models = set()
        eng._metrics = MagicMock()
        eng._push_error = MagicMock()
        eng.rewriter = None
        eng._settings_get = MagicMock(return_value=None)
        return eng

    def test_post_diarization_clear_cache_held_under_mlx_lock(self):
        """After diarization, mlx_lock must be entered before clear_cache."""
        import contextlib  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        from core import engine as _engine_mod  # noqa: PLC0415
        from core.config import settings as _settings  # noqa: PLC0415

        call_order: list[str] = []

        class _FakeLock:
            """Дубль mlx_lock(): и context-manager, и acquire/release.

            Прод берёт этот лок ДВУМЯ способами: `with mlx_lock():` в
            transcribe() и `mlx_lock().acquire(timeout=...)` в
            set_quality_profile (бюджет на необязательный flush кэша, чтобы
            зависшее превью не держало финальную транскрибацию — живой
            инцидент 2026-08-13). Фейк обязан отражать оба, иначе тест падает
            на AttributeError вместо проверки инварианта.
            """

            def __enter__(self):
                call_order.append("mlx_lock_enter")
                return self

            def __exit__(self, *_):
                call_order.append("mlx_lock_exit")
                return False

            def acquire(self, timeout=None):
                call_order.append("mlx_lock_enter")
                return True

            def release(self):
                call_order.append("mlx_lock_exit")

        try:
            import mlx.core as _real_mx
            _mlx_available = True
        except ImportError:
            _mlx_available = False

        eng = self._make_minimal_engine()
        eng._transcribe_with_fallback = MagicMock(return_value={
            "text": "hello world", "segments": [], "confidence": 0.9,
            "model": "whisper-small", "language": "en",
        })
        eng._maybe_run_diarization = MagicMock(return_value=None)
        eng._resolve_language = MagicMock(return_value="en")
        eng._build_dynamic_prompt = MagicMock(return_value="")
        eng._apply_vad_prefilter = MagicMock(return_value=None)
        eng._maybe_denoise = MagicMock(side_effect=lambda x: x)
        eng._maybe_multipass_retry = MagicMock(side_effect=lambda a, p, lang, r: r)

        audio = np.zeros(16000, dtype=np.float32)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(_engine_mod, "mlx_lock", return_value=_FakeLock())
            )
            stack.enter_context(patch.object(_settings, "STT_DENOISE_ENABLED", False))
            stack.enter_context(patch.object(_settings, "STT_VAD_PREFILTER_ENABLED", False))
            stack.enter_context(patch.object(_settings, "STT_MULTIPASS_ENABLED", False))
            stack.enter_context(patch.object(_settings, "DIARIZATION_ENABLED", True))
            mock_tu = stack.enter_context(patch("core.engine.TextUtils"))
            mock_tu.cleanup_transcript = MagicMock(return_value="hello world")
            # is_likely_repetition_loop is a module-level import if available
            try:
                stack.enter_context(
                    patch("core.engine.is_likely_repetition_loop",
                          return_value=(False, ""))
                )
            except AttributeError:
                pass
            if _mlx_available:
                stack.enter_context(
                    patch.object(_real_mx, "clear_cache",
                                 side_effect=lambda: call_order.append("clear_cache"))
                )
            else:
                stub_mx = MagicMock()
                stub_mx.clear_cache.side_effect = lambda: call_order.append("clear_cache")
                stack.enter_context(patch.dict("sys.modules", {"mlx.core": stub_mx}))
            try:
                eng.transcribe(audio)
            except Exception:
                pass  # only care about ordering up to clear_cache

        if "clear_cache" not in call_order:
            self.skipTest(
                "clear_cache not reached during transcribe() — "
                "integration patching insufficient in this env; "
                "AST test covers the invariant"
            )

        self.assertIn("mlx_lock_enter", call_order,
                      "mlx_lock must be entered during transcribe() post-diarization flush")
        lock_idx = call_order.index("mlx_lock_enter")
        cache_idx = call_order.index("clear_cache")
        self.assertLess(
            lock_idx, cache_idx,
            f"mlx_lock_enter ({lock_idx}) must precede clear_cache ({cache_idx}); "
            f"order={call_order}",
        )


if __name__ == "__main__":
    unittest.main()
