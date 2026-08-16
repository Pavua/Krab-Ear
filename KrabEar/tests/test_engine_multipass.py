"""Юнит-тесты для confidence-driven multi-pass STT retry.

Проверяет логику _maybe_multipass_retry / transcribe:
  - high confidence → no retry
  - low confidence → retry with max model
  - threshold == 0 → no retry ever
  - disabled (STT_MULTIPASS_ENABLED=False) → no retry
  - max_retries enforced
  - each attempt logged in metadata
  - best result (highest confidence) wins
  - remote fallback triggered when network_mode != offline_strict
  - preview mode → no retry regardless
  - retry model unavailable → skipped, attempts still recorded
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_GATE_PATCH = None


def setUpModule():
    """macOS часто отдаёт vm_pressure=warn; без патча retry-тесты ложно красные."""
    global _GATE_PATCH
    from unittest.mock import patch
    _GATE_PATCH = patch("core.engine.should_skip_second_mlx_checkpoint", return_value=False)
    _GATE_PATCH.start()


def tearDownModule():
    if _GATE_PATCH is not None:
        _GATE_PATCH.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    from core.engine import AudioEngine
    with patch("core.engine.threading.Thread.start", autospec=True):
        return AudioEngine()


def _segments_for_confidence(conf: float) -> list[dict]:
    """Построить segments с заданной средней уверенностью через avg_logprob."""
    import math
    logprob = math.log(max(conf, 1e-9))
    return [{"avg_logprob": logprob, "text": "hello"}]


def _stt_result(conf: float, model: str = "balanced-model") -> dict:
    return {
        "text": "hello",
        "segments": _segments_for_confidence(conf),
        "model_used": model,
        "language": "ru",
    }


# ---------------------------------------------------------------------------
# 1. High confidence — no retry
# ---------------------------------------------------------------------------

class HighConfidenceNoRetryTest(unittest.TestCase):
    """Первый pass с conf >= threshold → никаких retry."""

    def test_high_confidence_returns_immediately(self):
        engine = _make_engine()

        with patch.object(engine, "_transcribe_model") as mock_tm, \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            first = _stt_result(0.90)
            result = engine._maybe_multipass_retry(
                b"audio", "prompt", "ru", first
            )

        # _transcribe_model should NOT have been called for a retry
        mock_tm.assert_not_called()
        self.assertIn("multipass_attempts", result)
        self.assertEqual(len(result["multipass_attempts"]), 1)


# ---------------------------------------------------------------------------
# 2. Low confidence → retry with max model
# ---------------------------------------------------------------------------

class LowConfidenceRetryTest(unittest.TestCase):
    """conf 0.42 < threshold 0.65 → один retry с max model."""

    def test_low_confidence_triggers_retry(self):
        engine = _make_engine()
        max_result = _stt_result(0.80, model="max-model")

        with patch.object(engine, "_transcribe_model", return_value=max_result) as mock_tm, \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            first = _stt_result(0.42)
            result = engine._maybe_multipass_retry(
                b"audio", "prompt", "ru", first
            )

        mock_tm.assert_called_once()
        self.assertEqual(result["model_used"], "max-model")
        self.assertEqual(len(result["multipass_attempts"]), 2)
        self.assertEqual(result["multipass_attempts"][1]["model"], "max-model")


# ---------------------------------------------------------------------------
# 3. Threshold == 0 → never retry
# ---------------------------------------------------------------------------

class ZeroThresholdNoRetryTest(unittest.TestCase):
    """Threshold 0.0 → никогда не ретраить."""

    def test_zero_threshold_skips_retry(self):
        engine = _make_engine()

        with patch.object(engine, "_transcribe_model") as mock_tm, \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.0
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"

            first = _stt_result(0.10)  # very low, but threshold=0 → skip
            result = engine._maybe_multipass_retry(
                b"audio", "prompt", "ru", first
            )

        mock_tm.assert_not_called()
        # Returns original result unchanged (no multipass_attempts key added)
        self.assertNotIn("multipass_attempts", result)


# ---------------------------------------------------------------------------
# 4. Disabled (STT_MULTIPASS_ENABLED=False) → no retry via transcribe()
# ---------------------------------------------------------------------------

class MultipassDisabledTest(unittest.TestCase):
    """STT_MULTIPASS_ENABLED=False → _maybe_multipass_retry не вызывается."""

    def test_disabled_skips_multipass(self):
        engine = _make_engine()

        with patch.object(engine, "_maybe_multipass_retry") as mock_mp, \
             patch.object(engine, "_transcribe_with_fallback") as mock_fb, \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = False
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.TRANSCRIBE_LANGUAGE = "ru"
            mock_cfg.DIARIZATION_ENABLED = False
            mock_cfg.SMART_SILENCE_SKIP_ENABLED = False
            mock_cfg.MAX_AUDIO_MB = 1000
            mock_cfg.SENSEVOICE_EMOTION_TO_HISTORY = False
            mock_cfg.PIPELINE_V2 = False
            mock_cfg.TRANSCRIBE_PROMPT = "prompt"
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_fb.return_value = _stt_result(0.30)
            mock_cfg.LLM_ENABLED = False

            engine.transcribe(b"\x00" * 100, is_preview=False)

        mock_mp.assert_not_called()


# ---------------------------------------------------------------------------
# 5. max_retries enforced
# ---------------------------------------------------------------------------

class MaxRetriesEnforcedTest(unittest.TestCase):
    """Даже если все кандидаты доступны, делается не более STT_MAX_RETRIES попыток."""

    def test_max_retries_limits_calls(self):
        engine = _make_engine()
        # Two max models available + remote
        low_result = _stt_result(0.30, "max-model-A")

        with patch.object(engine, "_transcribe_model", return_value=low_result) as mock_tm, \
             patch.object(engine, "_transcribe_remote", return_value=low_result), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 1  # only 1 retry allowed
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model-A", "max-model-B"]
            mock_cfg.NETWORK_MODE = "online_preferred"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            first = _stt_result(0.20)
            engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        # Only 1 retry despite 2 max models + remote available
        self.assertEqual(mock_tm.call_count, 1)


# ---------------------------------------------------------------------------
# 6. Each attempt logged in metadata
# ---------------------------------------------------------------------------

class AttemptsMetadataTest(unittest.TestCase):
    """multipass_attempts содержит запись для каждой попытки."""

    def test_attempts_logged(self):
        engine = _make_engine()
        max_result = _stt_result(0.80, model="max-model")

        with patch.object(engine, "_transcribe_model", return_value=max_result), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            first = _stt_result(0.40)
            result = engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        attempts = result["multipass_attempts"]
        self.assertEqual(len(attempts), 2)
        for attempt in attempts:
            self.assertIn("model", attempt)
            self.assertIn("confidence", attempt)
            self.assertIn("latency_ms", attempt)
        self.assertEqual(attempts[0]["model"], first["model_used"])
        self.assertEqual(attempts[1]["model"], "max-model")


# ---------------------------------------------------------------------------
# 7. Best result wins (highest confidence)
# ---------------------------------------------------------------------------

class BestResultWinsTest(unittest.TestCase):
    """Если max model уверенность выше — возвращается max result."""

    def test_best_confidence_result_returned(self):
        engine = _make_engine()
        first = _stt_result(0.40, "balanced-model")
        max_result = _stt_result(0.85, "max-model")

        with patch.object(engine, "_transcribe_model", return_value=max_result), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            result = engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        self.assertEqual(result["model_used"], "max-model")


# ---------------------------------------------------------------------------
# 8. Remote fallback triggered when network_mode != offline_strict
# ---------------------------------------------------------------------------

class RemoteFallbackTest(unittest.TestCase):
    """Если max models не помогли → remote STT пробуется (network_mode online)."""

    def test_remote_called_when_online(self):
        engine = _make_engine()
        low_max = _stt_result(0.30, "max-model")
        remote_result = _stt_result(0.78, "remote")

        with patch.object(engine, "_transcribe_model", return_value=low_max), \
             patch.object(engine, "_transcribe_remote", return_value=remote_result) as mock_rem, \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "online_preferred"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            first = _stt_result(0.25)
            result = engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        mock_rem.assert_called_once()
        self.assertEqual(result["model_used"], "remote")


# ---------------------------------------------------------------------------
# 9. Preview mode → no retry
# ---------------------------------------------------------------------------

class PreviewNoRetryTest(unittest.TestCase):
    """is_preview=True → _maybe_multipass_retry не вызывается."""

    def test_preview_skips_multipass(self):
        engine = _make_engine()

        with patch.object(engine, "_maybe_multipass_retry") as mock_mp, \
             patch.object(engine, "_transcribe_with_fallback") as mock_fb, \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.TRANSCRIBE_LANGUAGE = "ru"
            mock_cfg.DIARIZATION_ENABLED = False
            mock_cfg.SMART_SILENCE_SKIP_ENABLED = False
            mock_cfg.MAX_AUDIO_MB = 1000
            mock_cfg.SENSEVOICE_EMOTION_TO_HISTORY = False
            mock_cfg.PIPELINE_V2 = False
            mock_cfg.TRANSCRIBE_PROMPT = "prompt"
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.LLM_ENABLED = False
            mock_fb.return_value = _stt_result(0.20)

            engine.transcribe(b"\x00" * 100, is_preview=True)

        mock_mp.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Retry model unavailable → skipped, attempts still recorded with error
# ---------------------------------------------------------------------------

class UnavailableModelSkippedTest(unittest.TestCase):
    """Если max model недоступна (Exception) → помечается unavailable, attempt записывается."""

    def test_unavailable_model_recorded_in_attempts(self):
        engine = _make_engine()

        with patch.object(engine, "_transcribe_model", side_effect=RuntimeError("OOM")), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            first = _stt_result(0.30)
            result = engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        attempts = result["multipass_attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertIn("error", attempts[1])
        self.assertIn("max-model", engine._unavailable_models)


class NativeEngineConfidenceTest(unittest.TestCase):
    """Результат без segments, но с явным confidence (GigaAM) — не 0.0.

    До фикса _raw_confidence_from_result возвращал 0.0 для любого результата
    без segments, из-за чего multipass ВСЕГДА перегонял GigaAM-результат через
    whisper и whisper побеждал — GigaAM был декоративным в каскаде.
    """

    def test_explicit_confidence_without_segments(self):
        engine = _make_engine()
        gigaam_result = {
            "text": "привет",
            "language": "ru",
            "confidence": 0.9,
            "engine": "gigaam-mlx-rnnt",
            "native_punctuation": True,
        }
        self.assertAlmostEqual(
            engine._raw_confidence_from_result(gigaam_result), 0.9
        )

    def test_no_segments_no_confidence_stays_zero(self):
        engine = _make_engine()
        self.assertEqual(
            engine._raw_confidence_from_result({"text": "..."}), 0.0
        )

    def test_segments_take_priority_over_explicit_confidence(self):
        engine = _make_engine()
        result = _stt_result(0.30)
        result["confidence"] = 0.99
        self.assertAlmostEqual(
            engine._raw_confidence_from_result(result), 0.30, places=2
        )

    def test_gigaam_result_above_threshold_skips_retry(self):
        engine = _make_engine()
        with patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"

            first = {
                "text": "привет",
                "language": "ru",
                "confidence": 0.9,
                "engine": "gigaam-mlx-rnnt",
                "native_punctuation": True,
            }
            result = engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        self.assertIs(result, first)
        self.assertEqual(len(result["multipass_attempts"]), 1)
        self.assertAlmostEqual(result["multipass_attempts"][0]["confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()
