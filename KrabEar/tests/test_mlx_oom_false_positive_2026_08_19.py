"""Regression test for a false-positive mlx.oom toast on IOGPUMetal assertions.

Bug (found live, not hypothetical — see docs/superpowers/specs history / CLAUDE.md
"Recurring bug classes" → sibling-gate asymmetry): `_transcribe_model` in
core/engine.py catches `(MemoryError, RuntimeError)` and classifies the message
into an ErrorBus code. The mlx.oom keyword set includes the bare substring
"metal", which is ALSO a substring of "iogpumetal" — so an IOGPUMetal
command-buffer assertion (Wave 64, self-recovers automatically via the
subprocess worker, is NOT an out-of-memory condition) pushed a false-positive
CRITICAL "не хватило памяти — выгрузи LM Studio" toast to the owner, in
addition to (or instead of, depending on code path) the correct
mlx.metal_assertion_failure classification.

Two independent call sites in `_transcribe_model` have this exception handler:
  1. worker-enabled path (KRAB_EAR_MLX_WHISPER_WORKER=1) — no watchdog wrapper.
  2. direct in-process path — wrapped by MLXWatchdog when recovery is enabled.

IMPORTANT: DO NOT import mlx_whisper or instantiate full AudioEngine —
memory constraint (yellow zone ~29 GB / 36 GB). All tests use mocked
collaborators, following the pattern in test_engine_mlx_timeout_variant_fallthrough_W1628.py.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Realistic messages — taken verbatim from the Wave 64 keyword set / test fixture
# already established in test_error_bus_phase_b_wave64.py, and from the real
# "Metal out of memory" phrasing used by test_worker_oom_detection.py.
_IOGPU_METAL_ASSERTION_MSG = (
    "IOGPUMetalCommandBuffer validate failed assertion (model=balanced)"
)
_GENUINE_OOM_MSG = "Metal out of memory: allocation failed"


def _make_engine_stub() -> object:
    """Build a minimal AudioEngine object without triggering any model loading."""
    from core.engine import AudioEngine

    engine = AudioEngine.__new__(AudioEngine)
    engine.current_model = "mlx-community/whisper-base-mlx"
    engine.quality_profile = "balanced"
    engine._unavailable_models = {}
    engine._error_bus = MagicMock()
    engine._llm_rewriter = None
    engine._settings_get = lambda k, d: d
    return engine


def _push_codes(engine) -> list[str]:
    return [c[0][0].code for c in engine._error_bus.push.call_args_list]


class WatchdogPathMetalAssertionTests(unittest.TestCase):
    """Direct in-process path (recovery_enabled=True → wrapped by MLXWatchdog)."""

    def _call(self, engine, exc, *, recovery_enabled: bool = True):
        import numpy as np

        audio_data = np.zeros(16000, dtype=np.float32)
        model_name = "mlx-community/whisper-base-mlx"

        watchdog_mock = MagicMock()
        watchdog_mock.run_with_timeout.side_effect = exc

        with (
            patch("core.engine.settings") as mock_settings,
            patch("core.engine.get_watchdog", return_value=watchdog_mock),
            patch("core.engine.mlx_lock") as mlx_lock_mock,
            patch("core.engine.mlx_inter_process_lock") as inter_lock_mock,
            patch("core.engine.mlx_whisper"),
        ):
            mlx_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            mlx_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            inter_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            inter_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            mock_settings.TRANSCRIBE_LANGUAGE = None
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = recovery_enabled
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 30.0

            with self.assertRaises(RuntimeError):
                engine._transcribe_model(audio_data, model_name, "")

    def test_iogpumetal_assertion_does_not_push_oom(self):
        """A GPU command-buffer assertion must NOT push the critical mlx.oom toast."""
        engine = _make_engine_stub()
        self._call(engine, RuntimeError(_IOGPU_METAL_ASSERTION_MSG))

        codes = _push_codes(engine)
        self.assertNotIn(
            "mlx.oom", codes,
            "IOGPUMetal assertion falsely classified as mlx.oom (false 'не хватило "
            "памяти' toast) — 'metal' keyword in oom set matches 'iogpumetal'",
        )

    def test_iogpumetal_assertion_pushes_metal_assertion_failure(self):
        """A GPU command-buffer assertion must push mlx.metal_assertion_failure."""
        engine = _make_engine_stub()
        self._call(engine, RuntimeError(_IOGPU_METAL_ASSERTION_MSG))

        codes = _push_codes(engine)
        self.assertIn("mlx.metal_assertion_failure", codes)

    def test_genuine_oom_still_pushes_mlx_oom(self):
        """A real allocation-failure message must still classify as mlx.oom."""
        engine = _make_engine_stub()
        self._call(engine, RuntimeError(_GENUINE_OOM_MSG))

        codes = _push_codes(engine)
        self.assertIn("mlx.oom", codes)
        self.assertNotIn("mlx.metal_assertion_failure", codes)


class WorkerPathMetalAssertionTests(unittest.TestCase):
    """Worker-enabled path (KRAB_EAR_MLX_WHISPER_WORKER=1) — no watchdog wrapper.

    Sibling of WatchdogPathMetalAssertionTests: same exception classification
    bug must be fixed in BOTH `_transcribe_model` code paths (sibling-gate
    asymmetry class — CLAUDE.md).
    """

    def _call(self, engine, exc):
        import numpy as np

        audio_data = np.zeros(16000, dtype=np.float32)
        model_name = "mlx-community/whisper-base-mlx"

        with (
            patch("core.engine.settings") as mock_settings,
            patch("core.engine.mlx_inter_process_lock") as inter_lock_mock,
            patch(
                "core.mlx_whisper_session.mlx_whisper_worker_enabled",
                return_value=True,
            ),
            patch(
                "core.mlx_whisper_session.transcribe_via_mlx_worker",
                side_effect=exc,
            ),
        ):
            inter_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            inter_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            mock_settings.TRANSCRIBE_LANGUAGE = None
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = True
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 30.0

            with self.assertRaises(RuntimeError):
                engine._transcribe_model(audio_data, model_name, "")

    def test_iogpumetal_assertion_does_not_push_oom(self):
        engine = _make_engine_stub()
        self._call(engine, RuntimeError(_IOGPU_METAL_ASSERTION_MSG))

        codes = _push_codes(engine)
        self.assertNotIn(
            "mlx.oom", codes,
            "worker-enabled path: IOGPUMetal assertion falsely classified as "
            "mlx.oom",
        )

    def test_iogpumetal_assertion_pushes_metal_assertion_failure(self):
        engine = _make_engine_stub()
        self._call(engine, RuntimeError(_IOGPU_METAL_ASSERTION_MSG))

        codes = _push_codes(engine)
        self.assertIn("mlx.metal_assertion_failure", codes)

    def test_genuine_oom_still_pushes_mlx_oom(self):
        engine = _make_engine_stub()
        self._call(engine, RuntimeError(_GENUINE_OOM_MSG))

        codes = _push_codes(engine)
        self.assertIn("mlx.oom", codes)
        self.assertNotIn("mlx.metal_assertion_failure", codes)


if __name__ == "__main__":
    unittest.main()
