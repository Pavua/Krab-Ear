"""Tests for W1604 F1 HIGH fix: MLXTimeoutError falls through to next variant
when recovery_enabled=True, instead of short-circuiting the variants loop.

W1628 fix verification:
  1. test_mlx_timeout_falls_through_to_next_variant_when_recovery_enabled
  2. test_mlx_timeout_after_all_variants_exhausted_raises
  3. test_no_regression_recovery_disabled_path

IMPORTANT: DO NOT import mlx_whisper or instantiate full AudioEngine —
memory constraint (yellow zone ~29 GB / 36 GB).
All tests use mocked collaborators.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.mlx_subprocess import MLXTimeoutError  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal AudioEngine stub — bypasses __init__ to avoid MLX/model loading
# ---------------------------------------------------------------------------

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


class TestMLXTimeoutVariantFallthrough(unittest.TestCase):
    """W1628: MLXTimeoutError is caught inside the variants loop so later
    variants are still attempted when recovery_enabled=True."""

    def _call_transcribe_model(
        self,
        engine,
        watchdog_side_effects,
        *,
        recovery_enabled: bool = True,
        timeout_sec: float = 30.0,
    ) -> object:
        """
        Invoke engine._transcribe_model with mocked collaborators.

        watchdog_side_effects: list of values / exceptions that
        get_watchdog().run_with_timeout() will raise or return, in order.
        """
        import numpy as np

        audio_data = np.zeros(16000, dtype=np.float32)
        model_name = "mlx-community/whisper-base-mlx"
        prompt = ""

        watchdog_mock = MagicMock()
        watchdog_mock.run_with_timeout.side_effect = watchdog_side_effects

        with (
            patch("core.engine.get_watchdog", return_value=watchdog_mock),
            patch("core.engine.mlx_lock"),  # make lock a no-op context
            patch("core.engine.mlx_whisper"),  # prevent any real import
            patch(
                "core.engine.getattr",
                side_effect=lambda obj, name, default=None: (
                    recovery_enabled if name == "MLX_CRASH_RECOVERY_ENABLED"
                    else timeout_sec if name == "MLX_TRANSCRIBE_TIMEOUT_SEC"
                    else default
                ),
            ) if False else None,  # handled via settings mock below
        ):
            # Patch settings attrs used by _transcribe_model
            with (
                patch("core.engine.settings") as mock_settings,
                patch("core.engine.get_watchdog", return_value=watchdog_mock),
                patch("core.engine.mlx_lock") as mlx_lock_mock,
            ):
                mlx_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
                mlx_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
                mock_settings.TRANSCRIBE_LANGUAGE = None
                mock_settings.MLX_CRASH_RECOVERY_ENABLED = recovery_enabled
                mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = timeout_sec

                return engine._transcribe_model(audio_data, model_name, prompt)

    # ------------------------------------------------------------------
    # Test 1: timeout on first variant → falls through to second variant
    # ------------------------------------------------------------------

    def test_mlx_timeout_falls_through_to_next_variant_when_recovery_enabled(self):
        """W1628: MLXTimeoutError on first variant must NOT abort the loop;
        second variant is tried and its result is returned."""
        engine = _make_engine_stub()

        timeout_exc = MLXTimeoutError(timeout_sec=30.0, model_name="mlx-community/whisper-base-mlx")
        success_result = {"segments": [{"text": "hello"}], "text": "hello"}

        # First call → timeout; second call → success
        side_effects = [timeout_exc, success_result]

        import numpy as np
        audio_data = np.zeros(16000, dtype=np.float32)
        model_name = "mlx-community/whisper-base-mlx"

        watchdog_mock = MagicMock()
        watchdog_mock.run_with_timeout.side_effect = side_effects

        with (
            patch("core.engine.settings") as mock_settings,
            patch("core.engine.get_watchdog", return_value=watchdog_mock),
            patch("core.engine.mlx_lock") as mlx_lock_mock,
            patch("core.engine.mlx_whisper"),
        ):
            mlx_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            mlx_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            mock_settings.TRANSCRIBE_LANGUAGE = None
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = True
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 30.0

            result = engine._transcribe_model(audio_data, model_name, "")

        # Second variant succeeded
        self.assertEqual(result["text"], "hello")

        # Watchdog called twice (first variant timed out, second succeeded)
        self.assertEqual(watchdog_mock.run_with_timeout.call_count, 2)

        # _push_error("stt.mlx_timeout", ...) must be called for the timed-out variant
        push_codes = [
            c[0][0].code
            for c in engine._error_bus.push.call_args_list
        ]
        self.assertIn("stt.mlx_timeout", push_codes)

    # ------------------------------------------------------------------
    # Test 2: all variants time out → MLXTimeoutError raised after loop
    # ------------------------------------------------------------------

    def test_mlx_timeout_after_all_variants_exhausted_raises(self):
        """W1628: When all variants hit MLXTimeoutError, the last one is re-raised."""
        engine = _make_engine_stub()

        timeout_exc = MLXTimeoutError(timeout_sec=30.0, model_name="mlx-community/whisper-base-mlx")

        import numpy as np
        audio_data = np.zeros(16000, dtype=np.float32)
        model_name = "mlx-community/whisper-base-mlx"

        watchdog_mock = MagicMock()
        # 3 variants → all time out
        watchdog_mock.run_with_timeout.side_effect = [timeout_exc, timeout_exc, timeout_exc]

        with (
            patch("core.engine.settings") as mock_settings,
            patch("core.engine.get_watchdog", return_value=watchdog_mock),
            patch("core.engine.mlx_lock") as mlx_lock_mock,
            patch("core.engine.mlx_whisper"),
        ):
            mlx_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            mlx_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            mock_settings.TRANSCRIBE_LANGUAGE = None
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = True
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 30.0

            with self.assertRaises(MLXTimeoutError):
                engine._transcribe_model(audio_data, model_name, "")

        # All 3 variants were tried
        self.assertEqual(watchdog_mock.run_with_timeout.call_count, 3)

        # _push_error must be called for each timed-out variant (3 times)
        push_codes = [
            c[0][0].code
            for c in engine._error_bus.push.call_args_list
        ]
        self.assertEqual(push_codes.count("stt.mlx_timeout"), 3)

    # ------------------------------------------------------------------
    # Test 3: no regression when recovery_enabled=False
    # ------------------------------------------------------------------

    def test_no_regression_recovery_disabled_path(self):
        """W1628: recovery_enabled=False path must be unaffected — direct
        mlx_whisper.transcribe() raises TypeError and falls through variants."""
        engine = _make_engine_stub()

        import numpy as np
        audio_data = np.zeros(16000, dtype=np.float32)
        model_name = "mlx-community/whisper-base-mlx"

        success_result = {"segments": [{"text": "test"}], "text": "test"}
        mlx_whisper_mock = MagicMock()
        # First call raises TypeError (unsupported param), second succeeds
        mlx_whisper_mock.transcribe.side_effect = [
            TypeError("unexpected keyword argument 'no_speech_threshold'"),
            success_result,
        ]

        with (
            patch("core.engine.settings") as mock_settings,
            patch("core.engine.mlx_lock") as mlx_lock_mock,
            patch("core.engine.mlx_whisper", mlx_whisper_mock),
            patch("core.engine.get_watchdog"),  # must NOT be called
        ):
            mlx_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            mlx_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            mock_settings.TRANSCRIBE_LANGUAGE = None
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = False
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 30.0

            result = engine._transcribe_model(audio_data, model_name, "")

        self.assertEqual(result["text"], "test")
        self.assertEqual(mlx_whisper_mock.transcribe.call_count, 2)

        # No stt.mlx_timeout error should be pushed in the disabled path
        push_codes = [
            c[0][0].code
            for c in engine._error_bus.push.call_args_list
        ]
        self.assertNotIn("stt.mlx_timeout", push_codes)


if __name__ == "__main__":
    unittest.main()
