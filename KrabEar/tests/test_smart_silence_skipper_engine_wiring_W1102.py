"""Tests for SmartSilenceSkipper wiring into AudioEngine pipeline (W1102).

Verifies:
- SmartSilenceSkipper.process() is called when SMART_SILENCE_SKIP_ENABLED=True
- SmartSilenceSkipper is skipped when SMART_SILENCE_SKIP_ENABLED=False
- VAD prefilter is skipped when SmartSilenceSkipper is active (mutex, W1096 F3)
- Soft-fail on SmartSilenceSkipper exception preserves original audio

All MLX, STT, and external calls are mocked — no real model loads.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_engine():
    from core.engine import AudioEngine
    return AudioEngine()


def _whisper_ok(text: str = "hello world") -> dict:
    return {
        "text": text,
        "segments": [{"avg_logprob": -0.2, "start": 0.0, "end": 1.0}],
        "engine": "mlx-whisper",
        "model_used": "fake/balanced",
        "language": "ru",
    }


def _make_audio(seconds: float = 3.0, sr: int = 16000) -> np.ndarray:
    """Return a short float32 numpy array of the given duration."""
    return np.zeros(int(seconds * sr), dtype=np.float32)


def _base_mock_cfg():
    """Return a MagicMock with all the settings attrs that transcribe() uses.

    All boolean flags are False and numeric settings are given real values so
    that comparisons like `audio_duration > settings.STT_STREAMING_MIN_AUDIO_SEC`
    don't raise TypeError.
    """
    cfg = MagicMock()
    # SmartSilenceSkipper (the flag being tested)
    cfg.SMART_SILENCE_SKIP_ENABLED = False
    # Pipeline gates — all disabled by default
    cfg.STT_VAD_PREFILTER_ENABLED = False
    cfg.STT_DENOISE_ENABLED = False
    cfg.STT_MULTIPASS_ENABLED = False
    cfg.DIARIZATION_ENABLED = False
    cfg.LLM_ENABLED = False
    cfg.PIPELINE_V2 = False
    cfg.PIPELINE_V2_ENABLED = False  # W1707: explicit False needed — MagicMock attribute is truthy
    cfg.STT_LANGUAGE_ROUTING_ENABLED = False
    cfg.STT_AUDIO_LANG_ID_ENABLED = False
    cfg.STT_SPEAKER_AWARE_PROMPT_ENABLED = False
    cfg.REALTIME_SILENCE_FILTER_ENABLED = False
    cfg.AUTO_GLOSSARY_ENABLED = False
    cfg.SENSEVOICE_ENABLED = False
    cfg.SENSEVOICE_EMOTION_TO_HISTORY = False
    cfg.STT_SENSEVOICE_ENABLED = False
    cfg.STT_CODE_SWITCHING_DETECT = False
    cfg.STT_GIGAAM_ENABLED = False
    cfg.VOICE_FINGERPRINT_ENABLED = False
    cfg.NUMBER_NORMALIZATION_ENABLED = False
    cfg.DATETIME_NORMALIZATION_ENABLED = False
    cfg.TELEGRAM_BRIDGE_ENABLED = False
    cfg.MLX_CRASH_RECOVERY_ENABLED = False
    cfg.STT_USE_RU_FINETUNE = False
    cfg.VOXTRAL_ENABLED = False
    cfg.WHISPERX_ENABLED = False
    cfg.PARAKEET_ENABLED = False
    cfg.STT_STREAMING_ENABLED = False
    cfg.STT_MULTIPASS_ENABLED = False
    # String settings
    cfg.TRANSCRIBE_LANGUAGE = "ru"
    cfg.TRANSCRIBE_PROMPT = ""
    cfg.MODEL_BALANCED = "fake/balanced"
    cfg.NETWORK_MODE = "offline_strict"
    cfg.SENTRY_DSN = ""
    cfg.SAY_VOICE = ""
    cfg.STT_DENOISE_STRENGTH = "moderate"
    cfg.STT_ROUTING = "auto_scored"
    cfg.STT_RU_FINETUNE_MODEL = "fake/ru"
    cfg.VOXTRAL_MODEL = "fake/voxtral"
    cfg.WHISPERX_MODEL = "fake/whisperx"
    cfg.WHISPERX_DEVICE = "cpu"
    cfg.PARAKEET_MODEL = "fake/parakeet"
    # Numeric settings (must be real numbers for comparisons to work)
    cfg.TRANSCRIBE_TIMEOUT_SEC = 30
    cfg.MAX_AUDIO_MB = 1000
    cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
    cfg.STT_MAX_RETRIES = 2
    cfg.STT_DIALOGUE_HINT_THRESHOLD = 2
    cfg.STT_DENOISE_SNR_THRESHOLD_DB = 15.0
    cfg.STT_STREAMING_MIN_AUDIO_SEC = 30.0
    cfg.STT_STREAMING_CHUNK_SEC = 15.0
    cfg.STT_STREAMING_OVERLAP_SEC = 2.0
    cfg.STT_VAD_SILENCE_TRIM_THRESHOLD_SEC = 2.0
    cfg.STT_AUDIO_LANG_ID_PREVIEW_SEC = 5.0
    cfg.STT_CODE_SWITCHING_THRESHOLD = 0.1
    # List settings
    cfg.model_max_list = ["fake/max"]
    cfg.STT_HOTWORDS = []
    cfg.LLM_FALLBACK_CHAIN = []
    # Path settings (cast to string-like)
    import pathlib
    cfg.DATA_DIR = pathlib.Path("/tmp/krab_ear_test")
    return cfg


class SmartSilenceSkipperEngineWiringTests(unittest.TestCase):
    """Tests for step 2.6 SmartSilenceSkipper wiring in engine.py (W1102)."""

    # ------------------------------------------------------------------
    # Test 1: SmartSilenceSkipper is called when enabled
    # ------------------------------------------------------------------

    def test_smart_silence_skipper_called_when_enabled(self):
        """When SMART_SILENCE_SKIP_ENABLED=True, SmartSilenceSkipper.process() is called."""
        audio = _make_audio(3.0)

        from core.smart_silence_skipper import SkipResult

        fake_result = SkipResult(
            processed_audio=audio.copy(),
            original_duration_sec=3.0,
            processed_duration_sec=2.5,
            skipped_segments=[{"start": 1.0, "end": 1.5, "duration": 0.5}],
            time_saved_sec=0.5,
            time_saved_pct=16.67,
        )

        mock_sss_instance = MagicMock()
        mock_sss_instance.process.return_value = fake_result
        mock_sss_cls = MagicMock(return_value=mock_sss_instance)

        cfg = _base_mock_cfg()
        cfg.SMART_SILENCE_SKIP_ENABLED = True

        engine = _make_engine()
        with patch("core.engine.settings", cfg), \
             patch("core.smart_silence_skipper.SmartSilenceSkipper", mock_sss_cls), \
             patch.object(engine, "_transcribe_with_fallback", return_value=_whisper_ok()), \
             patch.object(engine, "_maybe_run_diarization", return_value=None):
            engine.transcribe(audio)

        # SmartSilenceSkipper was instantiated and process() was called
        mock_sss_cls.assert_called_once()
        mock_sss_instance.process.assert_called_once()
        # Verify sample_rate=16000 was passed
        _call = mock_sss_instance.process.call_args
        # Either positional or keyword
        passed_sr = _call[1].get("sample_rate") if _call[1] else (
            _call[0][1] if len(_call[0]) > 1 else None
        )
        self.assertEqual(passed_sr, 16000)

    # ------------------------------------------------------------------
    # Test 2: SmartSilenceSkipper is NOT called when disabled
    # ------------------------------------------------------------------

    def test_smart_silence_skipper_skipped_when_disabled(self):
        """When SMART_SILENCE_SKIP_ENABLED=False, SmartSilenceSkipper is never touched."""
        audio = _make_audio(3.0)

        mock_sss_cls = MagicMock()
        cfg = _base_mock_cfg()
        cfg.SMART_SILENCE_SKIP_ENABLED = False

        engine = _make_engine()
        with patch("core.engine.settings", cfg), \
             patch("core.smart_silence_skipper.SmartSilenceSkipper", mock_sss_cls), \
             patch.object(engine, "_transcribe_with_fallback", return_value=_whisper_ok()), \
             patch.object(engine, "_maybe_run_diarization", return_value=None):
            engine.transcribe(audio)

        # SmartSilenceSkipper should NOT have been instantiated
        mock_sss_cls.assert_not_called()

    # ------------------------------------------------------------------
    # Test 3: VAD prefilter skipped when SmartSilenceSkipper is active (mutex)
    # ------------------------------------------------------------------

    def test_vad_prefilter_skipped_when_smart_silence_active(self):
        """When SmartSilenceSkipper runs successfully, _apply_vad_prefilter is NOT called."""
        audio = _make_audio(3.0)

        from core.smart_silence_skipper import SkipResult

        fake_sss_result = SkipResult(
            processed_audio=audio.copy(),
            original_duration_sec=3.0,
            processed_duration_sec=2.8,
        )

        mock_sss_instance = MagicMock()
        mock_sss_instance.process.return_value = fake_sss_result
        mock_sss_cls = MagicMock(return_value=mock_sss_instance)

        cfg = _base_mock_cfg()
        cfg.SMART_SILENCE_SKIP_ENABLED = True
        cfg.STT_VAD_PREFILTER_ENABLED = True  # VAD also enabled — mutex must suppress it

        engine = _make_engine()
        with patch("core.engine.settings", cfg), \
             patch("core.smart_silence_skipper.SmartSilenceSkipper", mock_sss_cls), \
             patch.object(engine, "_transcribe_with_fallback", return_value=_whisper_ok()), \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch.object(engine, "_apply_vad_prefilter") as mock_vad:
            engine.transcribe(audio)

        # SmartSilenceSkipper ran
        mock_sss_instance.process.assert_called_once()
        # VAD prefilter must NOT have been called due to mutex
        mock_vad.assert_not_called()

    # ------------------------------------------------------------------
    # Test 4: Soft-fail on exception preserves original audio
    # ------------------------------------------------------------------

    def test_smart_silence_skipper_soft_fail_preserves_audio(self):
        """If SmartSilenceSkipper raises, original audio is passed to STT unchanged."""
        audio = _make_audio(3.0)

        captured_audio = {}

        def fake_transcribe(audio_arg, prompt=None, language=None):
            captured_audio["audio"] = audio_arg
            return _whisper_ok()

        mock_sss_instance = MagicMock()
        mock_sss_instance.process.side_effect = RuntimeError("GPU exploded")
        mock_sss_cls = MagicMock(return_value=mock_sss_instance)

        cfg = _base_mock_cfg()
        cfg.SMART_SILENCE_SKIP_ENABLED = True

        engine = _make_engine()
        with patch("core.engine.settings", cfg), \
             patch("core.smart_silence_skipper.SmartSilenceSkipper", mock_sss_cls), \
             patch.object(engine, "_transcribe_with_fallback",
                          side_effect=fake_transcribe), \
             patch.object(engine, "_maybe_run_diarization", return_value=None):
            result = engine.transcribe(audio)

        # STT still ran (soft-fail, not abort)
        self.assertIn("text", result)
        # Audio passed to STT must be the original (unmodified on exception)
        self.assertIn("audio", captured_audio)
        np.testing.assert_array_equal(captured_audio["audio"], audio)

    # ------------------------------------------------------------------
    # Test 5: SmartSilenceSkipper not called for is_preview=True
    # ------------------------------------------------------------------

    def test_smart_silence_skipper_skipped_in_preview_mode(self):
        """SmartSilenceSkipper is never called when is_preview=True."""
        audio = _make_audio(3.0)

        mock_sss_cls = MagicMock()
        cfg = _base_mock_cfg()
        cfg.SMART_SILENCE_SKIP_ENABLED = True

        engine = _make_engine()
        with patch("core.engine.settings", cfg), \
             patch("core.smart_silence_skipper.SmartSilenceSkipper", mock_sss_cls), \
             patch.object(engine, "_transcribe_with_fallback", return_value=_whisper_ok()), \
             patch.object(engine, "_maybe_run_diarization", return_value=None):
            engine.transcribe(audio, is_preview=True)

        # is_preview=True must skip SmartSilenceSkipper
        mock_sss_cls.assert_not_called()

    # ------------------------------------------------------------------
    # Test 6: VAD prefilter runs normally when SmartSilenceSkipper disabled
    # ------------------------------------------------------------------

    def test_vad_prefilter_runs_when_smart_silence_disabled(self):
        """When SmartSilenceSkipper disabled, VAD prefilter runs if configured."""
        audio = _make_audio(3.0)

        cfg = _base_mock_cfg()
        cfg.SMART_SILENCE_SKIP_ENABLED = False
        cfg.STT_VAD_PREFILTER_ENABLED = True

        engine = _make_engine()
        with patch("core.engine.settings", cfg), \
             patch.object(engine, "_transcribe_with_fallback", return_value=_whisper_ok()), \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch.object(engine, "_apply_vad_prefilter",
                          return_value=audio) as mock_vad:
            engine.transcribe(audio)

        # VAD prefilter should have been called since SmartSilenceSkipper is off
        mock_vad.assert_called_once_with(audio)


if __name__ == "__main__":
    unittest.main()
