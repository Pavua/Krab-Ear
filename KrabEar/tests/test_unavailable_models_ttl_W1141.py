"""Tests for W1137 F2 (W1141): _unavailable_models TTL + reset IPC.

Verified with AST + unittest (no mlx, no sounddevice, no backend deps needed).
"""
from __future__ import annotations

import sys
import os
import time
import types
import unittest

# ---------------------------------------------------------------------------
# Path setup — allow `from core.engine import ...` when run standalone
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
for p in (PROJECT_ROOT, KRAB_EAR_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so engine.py imports cleanly in test env
# ---------------------------------------------------------------------------


def _install_stub(name: str, attrs: dict | None = None) -> types.ModuleType:
    """Install a stub module if not already present."""
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if attrs:
            for k, v in attrs.items():
                setattr(mod, k, v)
        sys.modules[name] = mod
    return sys.modules[name]


# mlx and mlx_whisper
_install_stub("mlx")
_install_stub("mlx.core")
_install_stub("mlx_whisper")

# numpy — usually present; stub if not
try:
    import numpy  # noqa: F401
except ImportError:
    _install_stub("numpy")

# soundfile
_install_stub("soundfile")

# torch and friends
_install_stub("torch")
_install_stub("torch.nn")
_install_stub("torch.nn.functional")

# requests
_install_stub("requests")

# pyannote
_install_stub("pyannote")
_install_stub("pyannote.audio")

# funasr
_install_stub("funasr")

# core dependencies that engine.py imports
_install_stub("core.mlx_lock", {"mlx_lock": lambda: None})
_install_stub("core.mlx_subprocess", {"MLXTimeoutError": Exception, "get_watchdog": lambda: None})

# Stub settings singleton
_settings_mod = _install_stub("core.config")
_settings_mod.settings = types.SimpleNamespace(
    MODEL_BALANCED="whisper-balanced",
    model_max_list=["whisper-large-v3"],
    TRANSCRIBE_TIMEOUT_SEC=30,
    NETWORK_MODE="offline_strict",
    STT_USE_RU_FINETUNE=False,
    PARAKEET_ENABLED=False,
    SENSEVOICE_ENABLED=False,
    WHISPERX_ENABLED=False,
    VOXTRAL_ENABLED=False,
    STT_GIGAAM_ENABLED=False,
)

_install_stub("core.confidence_calibrator", {
    "ConfidenceCalibrator": lambda: types.SimpleNamespace(calibrate=lambda c: c),
})
_install_stub("core.text_diff", {
    "TextDiffAnalyzer": type("TextDiffAnalyzer", (), {"diff": lambda self, a, b: {}}),
})
_install_stub("core.utils", {
    "TextUtils": type("TextUtils", (), {}),
    "is_likely_repetition_loop": lambda t: False,
})
_install_stub("core.transcript_context", {"build_initial_prompt": lambda *a, **kw: ""})
_install_stub("backend.performance_profiler", {
    "profiler": types.SimpleNamespace(
        start_span=lambda name: types.SimpleNamespace(
            __enter__=lambda s: s,
            __exit__=lambda s, *a: False,
        ),
    ),
})


# ---------------------------------------------------------------------------
# Now we can safely import engine pieces we need
# ---------------------------------------------------------------------------
from core.engine import AudioEngine, _UNAVAILABLE_TTL_SEC  # noqa: E402


class _MinimalEngine:
    """Minimal AudioEngine instance without full init (avoids sounddevice etc.)."""

    def __new__(cls):  # type: ignore[override]
        obj = object.__new__(cls)
        # Inject only the attributes we need for TTL tests
        obj._unavailable_models = {}
        obj._RU_FINETUNE_MARKER = "__ru_finetune__"
        obj._GIGAAM_MARKER = "__gigaam__"
        obj._PARAKEET_MARKER = "__parakeet__"
        obj._SENSEVOICE_MARKER = "__sensevoice__"
        obj._WHISPERX_MARKER = "__whisperx__"
        obj._VOXTRAL_MARKER = "__voxtral__"
        return obj

    # Bind real TTL methods from AudioEngine
    _mark_model_unavailable = AudioEngine._mark_model_unavailable
    _is_model_unavailable = AudioEngine._is_model_unavailable
    reset_unavailable_models = AudioEngine.reset_unavailable_models


class TestUnavailableModelTTL(unittest.TestCase):
    """Unit tests for TTL-based _unavailable_models."""

    def _make_engine(self) -> _MinimalEngine:
        return _MinimalEngine()  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # test_unavailable_model_retried_after_ttl
    # ------------------------------------------------------------------

    def test_unavailable_model_retried_after_ttl(self) -> None:
        """After TTL expires, _is_model_unavailable returns False."""
        eng = self._make_engine()
        model = "whisper-large-v3"

        eng._mark_model_unavailable(model)
        # Immediately after marking: should be unavailable
        self.assertTrue(eng._is_model_unavailable(model))

        # Artificially expire the TTL by backdating the eviction time
        eng._unavailable_models[model] = time.time() - 1.0  # 1 second in the past

        # Now it should be considered available again
        self.assertFalse(eng._is_model_unavailable(model))

        # And the entry should have been cleaned up (lazy deletion)
        self.assertNotIn(model, eng._unavailable_models)

    def test_model_remains_unavailable_before_ttl(self) -> None:
        """Model stays unavailable while TTL has not yet expired."""
        eng = self._make_engine()
        model = "whisper-large-v3-mlx"

        eng._mark_model_unavailable(model)
        # TTL is 1800 s; right after marking it must still be unavailable
        self.assertTrue(eng._is_model_unavailable(model))

    def test_mark_sets_future_expiry(self) -> None:
        """_mark_model_unavailable stores expiry ≈ now + TTL."""
        eng = self._make_engine()
        model = "my-model"
        before = time.time()
        eng._mark_model_unavailable(model)
        after = time.time()

        expiry = eng._unavailable_models[model]
        self.assertGreaterEqual(expiry, before + _UNAVAILABLE_TTL_SEC)
        self.assertLessEqual(expiry, after + _UNAVAILABLE_TTL_SEC + 0.1)

    # ------------------------------------------------------------------
    # test_reset_unavailable_models_ipc
    # ------------------------------------------------------------------

    def test_reset_unavailable_models_ipc(self) -> None:
        """reset_unavailable_models clears all entries and returns cleared list."""
        eng = self._make_engine()
        eng._mark_model_unavailable("model-a")
        eng._mark_model_unavailable("model-b")
        eng._mark_model_unavailable("model-c")

        result = eng.reset_unavailable_models()

        self.assertEqual(result["count"], 3)
        self.assertCountEqual(result["cleared"], ["model-a", "model-b", "model-c"])
        self.assertEqual(len(eng._unavailable_models), 0)

    def test_reset_unavailable_models_empty(self) -> None:
        """reset_unavailable_models on empty dict returns count=0."""
        eng = self._make_engine()
        result = eng.reset_unavailable_models()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["cleared"], [])

    def test_reset_makes_model_available_immediately(self) -> None:
        """After reset, previously unavailable model is accessible."""
        eng = self._make_engine()
        model = "whisper-turbo"
        eng._mark_model_unavailable(model)
        self.assertTrue(eng._is_model_unavailable(model))

        eng.reset_unavailable_models()

        self.assertFalse(eng._is_model_unavailable(model))

    # ------------------------------------------------------------------
    # test_unavailable_models_bounded_growth
    # ------------------------------------------------------------------

    def test_unavailable_models_bounded_growth(self) -> None:
        """Dict doesn't grow after TTL expires — lazy cleanup removes expired entries."""
        eng = self._make_engine()
        models = [f"model-{i}" for i in range(20)]

        # Mark all unavailable
        for m in models:
            eng._mark_model_unavailable(m)
        self.assertEqual(len(eng._unavailable_models), 20)

        # Expire all TTLs
        for m in models:
            eng._unavailable_models[m] = time.time() - 1.0

        # Checking each model triggers lazy deletion
        for m in models:
            self.assertFalse(eng._is_model_unavailable(m))

        # Dict should now be empty
        self.assertEqual(len(eng._unavailable_models), 0)

    def test_new_model_does_not_inherit_expiry(self) -> None:
        """Marking the same model twice resets its TTL (not additive)."""
        eng = self._make_engine()
        model = "whisper-large-v3"

        eng._mark_model_unavailable(model)
        first_expiry = eng._unavailable_models[model]

        # Simulate time passing, then mark again
        eng._unavailable_models[model] = time.time() - 100
        eng._mark_model_unavailable(model)
        second_expiry = eng._unavailable_models[model]

        self.assertGreater(second_expiry, first_expiry)

    def test_is_unavailable_returns_false_for_unknown_model(self) -> None:
        """_is_model_unavailable returns False for models never marked."""
        eng = self._make_engine()
        self.assertFalse(eng._is_model_unavailable("unknown-model"))

    def test_ttl_constant_value(self) -> None:
        """_UNAVAILABLE_TTL_SEC is set to 1800 seconds (30 minutes)."""
        self.assertEqual(_UNAVAILABLE_TTL_SEC, 1800.0)


if __name__ == "__main__":
    unittest.main()
