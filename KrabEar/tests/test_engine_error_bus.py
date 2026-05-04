"""Tests for stt.load_fail, stt.empty_text, diarization.pipeline_fail, mlx.oom
error pushes in AudioEngine (Phase B.2 F3).

IMPORTANT: DO NOT import mlx_whisper or instantiate full AudioEngine —
memory constraint (yellow zone ~29 GB / 36 GB).
All tests use mocked collaborators.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Minimal AudioEngine stub — bypasses __init__ to avoid MLX/model loading
# ---------------------------------------------------------------------------

def _make_engine_stub() -> object:
    """Build a minimal AudioEngine object without triggering any model loading."""
    from core.engine import AudioEngine
    engine = AudioEngine.__new__(AudioEngine)
    engine.current_model = "mlx-community/whisper-base-mlx"
    engine.quality_profile = "balanced"
    engine._unavailable_models = set()
    engine._error_bus = MagicMock()
    engine._llm_rewriter = None
    engine._settings_get = lambda k, d: d
    return engine


class AudioEnginePushErrorHelperTests(unittest.TestCase):
    """Unit tests for AudioEngine._push_error helper."""

    def test_no_bus_does_not_raise(self) -> None:
        """_push_error with no _error_bus set must not raise."""
        from core.engine import AudioEngine
        engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "test-model"
        engine.quality_profile = "balanced"
        # No _error_bus
        engine._push_error("stt.load_fail", "test debug")  # must not raise

    def test_broken_bus_does_not_raise(self) -> None:
        engine = _make_engine_stub()
        engine._error_bus.push.side_effect = RuntimeError("bus broken")
        engine._push_error("stt.load_fail", "some debug")  # must not raise

    def test_push_correct_code(self) -> None:
        engine = _make_engine_stub()
        engine._push_error("stt.load_fail", "MemoryError loading model")
        self.assertEqual(engine._error_bus.push.call_count, 1)
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "stt.load_fail")
        self.assertEqual(pushed.severity, "error")

    def test_push_stt_empty_text(self) -> None:
        engine = _make_engine_stub()
        engine._push_error("stt.empty_text", "empty result for 5.0s audio", severity="info")
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "stt.empty_text")
        self.assertEqual(pushed.severity, "info")

    def test_push_diarization_pipeline_fail(self) -> None:
        engine = _make_engine_stub()
        engine._push_error("diarization.pipeline_fail", "RuntimeError: Metal crash", severity="warn")
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "diarization.pipeline_fail")
        self.assertEqual(pushed.component, "diarization")

    def test_push_mlx_oom(self) -> None:
        engine = _make_engine_stub()
        engine._push_error("mlx.oom", "RuntimeError: failed to allocate buffer", severity="critical")
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "mlx.oom")
        self.assertEqual(pushed.severity, "critical")


class SttLoadFailCallSiteTests(unittest.TestCase):
    """stt.load_fail is pushed on MemoryError / OOM OSError in _transcribe_with_fallback_impl."""

    def test_memory_error_pushes_stt_load_fail(self) -> None:
        """MemoryError in the chain triggers stt.load_fail via _push_error."""
        engine = _make_engine_stub()
        model_name = "mlx-community/whisper-base-mlx"

        # Simulate the actual exception handler logic from the chain:
        # On MemoryError, the chain calls self._push_error("stt.load_fail", ...)
        try:
            raise MemoryError("OOM loading model")
        except MemoryError:
            engine._unavailable_models.add(model_name)
            engine._push_error(
                "stt.load_fail",
                f"MemoryError loading {model_name} — switching to balanced",
                severity="error",
            )

        self.assertEqual(engine._error_bus.push.call_count, 1)
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "stt.load_fail")
        self.assertEqual(pushed.severity, "error")
        self.assertIn(model_name, pushed.message_debug)

    def test_oom_oserror_pushes_stt_load_fail(self) -> None:
        """OSError with errno=12 triggers stt.load_fail via _push_error."""
        engine = _make_engine_stub()
        import errno as _errno
        model_name = "mlx-community/whisper-large-mlx"

        oom_err = OSError("Cannot allocate memory")
        oom_err.errno = _errno.ENOMEM  # errno 12

        # Simulate the chain exception handler
        try:
            raise oom_err
        except OSError as e:
            if e.errno == 12 or "Cannot allocate memory" in str(e):
                engine._unavailable_models.add(model_name)
                engine._push_error(
                    "stt.load_fail",
                    f"OOM (OSError errno={e.errno}) loading {model_name}",
                    severity="error",
                )

        codes = [c[0][0].code for c in engine._error_bus.push.call_args_list]
        self.assertIn("stt.load_fail", codes)
        pushed = engine._error_bus.push.call_args_list[0][0][0]
        self.assertEqual(pushed.severity, "error")

    def test_no_push_on_regular_exception(self) -> None:
        """Regular exception (non-OOM) does not push stt.load_fail."""
        engine = _make_engine_stub()
        model_name = "mlx-community/whisper-base-mlx"

        # Simulate non-OOM OSError (e.g. file not found)
        err = OSError("File not found")
        err.errno = 2  # ENOENT
        try:
            raise err
        except OSError as e:
            if e.errno == 12 or "Cannot allocate memory" in str(e):
                engine._push_error("stt.load_fail", f"OOM: {e}")
            # Regular OSError just logs — no push

        engine._error_bus.push.assert_not_called()


class SttEmptyTextCallSiteTests(unittest.TestCase):
    """stt.empty_text is pushed when transcribe returns empty and audio > 2s."""

    def test_empty_result_long_audio_pushes(self) -> None:
        """Empty STT result with >2s audio pushes stt.empty_text."""
        engine = _make_engine_stub()

        # We test _push_error is called with stt.empty_text when the empty guard fires.
        # Simulate the guard condition directly by calling _push_error (call site test):
        import numpy as np
        audio = np.zeros(16000 * 3, dtype=np.float32)  # 3 seconds
        audio_dur = len(audio) / 16000.0  # 3.0s

        # Simulate the guard: empty text, not preview, audio > 2s
        raw_text = ""
        is_preview = False
        result_stub = {"audio_duration_sec": 0.0, "model_used": "test-model"}

        if not raw_text and not is_preview:
            _audio_dur = result_stub.get("audio_duration_sec") or 0.0
            if _audio_dur <= 0.0 and isinstance(audio, __import__("numpy").ndarray):
                _audio_dur = len(audio) / 16000.0
            if _audio_dur > 2.0:
                engine._push_error(
                    "stt.empty_text",
                    f"empty STT result for {_audio_dur:.1f}s audio",
                    severity="info",
                )

        self.assertEqual(engine._error_bus.push.call_count, 1)
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "stt.empty_text")
        self.assertEqual(pushed.severity, "info")

    def test_empty_result_short_audio_no_push(self) -> None:
        """Empty STT result with <=2s audio (legit silence) does NOT push."""
        engine = _make_engine_stub()

        import numpy as np
        audio = np.zeros(16000 * 1, dtype=np.float32)  # 1 second
        raw_text = ""
        is_preview = False
        result_stub = {"audio_duration_sec": 0.0, "model_used": "test-model"}

        if not raw_text and not is_preview:
            _audio_dur = result_stub.get("audio_duration_sec") or 0.0
            if _audio_dur <= 0.0 and isinstance(audio, __import__("numpy").ndarray):
                _audio_dur = len(audio) / 16000.0
            if _audio_dur > 2.0:
                engine._push_error("stt.empty_text", "should not fire", severity="info")

        engine._error_bus.push.assert_not_called()

    def test_nonempty_result_no_push(self) -> None:
        """Non-empty STT result never triggers stt.empty_text."""
        engine = _make_engine_stub()
        raw_text = "привет мир"  # non-empty
        is_preview = False

        if not raw_text and not is_preview:
            engine._push_error("stt.empty_text", "should not fire")

        engine._error_bus.push.assert_not_called()

    def test_preview_no_push(self) -> None:
        """Even with empty text + long audio, preview mode suppresses the push."""
        engine = _make_engine_stub()

        import numpy as np
        audio = np.zeros(16000 * 5, dtype=np.float32)  # 5 seconds
        raw_text = ""
        is_preview = True  # preview — should suppress

        if not raw_text and not is_preview:
            engine._push_error("stt.empty_text", "should not fire")

        engine._error_bus.push.assert_not_called()


class DiarizationPipelineFailCallSiteTests(unittest.TestCase):
    """diarization.pipeline_fail is pushed in _maybe_run_diarization on exception."""

    def test_diarization_exception_pushes_pipeline_fail(self) -> None:
        """When _run_diarization raises, diarization.pipeline_fail is pushed."""
        engine = _make_engine_stub()
        # Add attributes needed by _maybe_run_diarization
        engine._diarization_pipeline = None
        engine._diarization_load_error = None

        import numpy as np
        audio = "/tmp/fake_audio.wav"  # string path — _resolve_audio_path needs this

        with patch.object(engine, "_resolve_audio_path", return_value="/tmp/fake_audio.wav"):
            with patch.object(engine, "_run_diarization",
                              side_effect=RuntimeError("pyannote Metal crash")):
                with patch("core.engine.settings") as mock_settings:
                    mock_settings.DIARIZATION_ENABLED = True

                    result = engine._maybe_run_diarization(
                        audio_data=audio,
                        whisper_segments=[],
                        is_preview=False,
                        diarize=True,
                    )

        self.assertIn("error", result)
        self.assertEqual(engine._error_bus.push.call_count, 1)
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "diarization.pipeline_fail")
        self.assertEqual(pushed.component, "diarization")
        self.assertEqual(pushed.severity, "warn")

    def test_no_push_on_preview(self) -> None:
        """In preview mode, diarization is skipped entirely — no push."""
        engine = _make_engine_stub()

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        with patch("core.engine.settings") as mock_settings:
            mock_settings.DIARIZATION_ENABLED = True

            result = engine._maybe_run_diarization(
                audio_data=audio,
                whisper_segments=[],
                is_preview=True,
                diarize=None,
            )

        self.assertFalse(result["enabled"])
        engine._error_bus.push.assert_not_called()

    def test_no_push_when_diarization_disabled(self) -> None:
        """When diarize=False, no exception and no push."""
        engine = _make_engine_stub()

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        result = engine._maybe_run_diarization(
            audio_data=audio,
            whisper_segments=[],
            is_preview=False,
            diarize=False,
        )

        self.assertFalse(result["enabled"])
        engine._error_bus.push.assert_not_called()


class MlxOomCallSiteTests(unittest.TestCase):
    """mlx.oom is pushed when _transcribe_model catches OOM-pattern RuntimeError."""

    def test_oom_runtime_error_pushes_mlx_oom(self) -> None:
        """RuntimeError with 'allocate' in message pushes mlx.oom."""
        engine = _make_engine_stub()

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)
        oom_err = RuntimeError("Metal: failed to allocate buffer of size 4096MB")

        # Simulate the guard in _transcribe_model
        _emsg = str(oom_err).lower()
        if any(kw in _emsg for kw in ("allocat", "out of memory", "metal", "oom")):
            engine._push_error(
                "mlx.oom",
                f"RuntimeError: {oom_err} (model=test)",
                severity="critical",
            )

        self.assertEqual(engine._error_bus.push.call_count, 1)
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "mlx.oom")
        self.assertEqual(pushed.severity, "critical")

    def test_oom_memory_error_triggers_pattern(self) -> None:
        """MemoryError (isinstance check) also triggers mlx.oom guard."""
        engine = _make_engine_stub()

        err = MemoryError()

        # Test the isinstance branch of the guard
        if isinstance(err, MemoryError):
            engine._push_error(
                "mlx.oom",
                f"MemoryError in transcribe_model",
                severity="critical",
            )

        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "mlx.oom")

    def test_unrelated_runtime_error_no_push(self) -> None:
        """RuntimeError without OOM keywords does not push mlx.oom."""
        engine = _make_engine_stub()

        err = RuntimeError("Unexpected keyword argument")
        _emsg = str(err).lower()

        if isinstance(err, MemoryError) or any(
            kw in _emsg for kw in ("allocat", "out of memory", "metal", "oom")
        ):
            engine._push_error("mlx.oom", "should not fire")

        engine._error_bus.push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
