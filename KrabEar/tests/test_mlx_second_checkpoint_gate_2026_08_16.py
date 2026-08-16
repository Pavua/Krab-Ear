"""P0 2026-08-16: не грузить второй MLX Whisper-чекпоинт под vm_pressure.

Живой инцидент: ai.krab.ear.rest, каскад balanced (turbo) → retry large-v3-mlx
→ retry turbo при confidence < 0.65, параллельно 10 LM Studio MLX на 36 ГБ →
SIGSEGV в потоке whisper-large-v3-turbo. Handoff:
docs/HANDOFF_WHISPER_TURBO_SEGV_2026-08-16_RU.md
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _segments_for_confidence(conf: float) -> list[dict]:
    logprob = math.log(max(conf, 1e-9))
    return [{"avg_logprob": logprob, "text": "hello"}]


def _stt_result(conf: float, model: str = "mlx-community/whisper-large-v3-turbo") -> dict:
    return {
        "text": "hello",
        "segments": _segments_for_confidence(conf),
        "model_used": model,
        "language": "ru",
    }


def _make_engine():
    from core.engine import AudioEngine
    with patch("core.engine.threading.Thread.start", autospec=True):
        return AudioEngine()


class VmPressureProbeTest(unittest.TestCase):
    def test_skip_when_pressure_warn_or_higher(self):
        from core.mlx_memory_gate import should_skip_second_mlx_checkpoint

        with patch("core.mlx_memory_gate.vm_pressure_level", return_value=1), \
             patch.dict("os.environ", {}, clear=False):
            # Явно снимаем принудительные env, если они есть в оболочке агента.
            with patch.dict("os.environ", {"KRAB_EAR_STT_SKIP_SECOND_MLX": ""}, clear=False):
                self.assertTrue(should_skip_second_mlx_checkpoint())

    def test_no_skip_when_probe_unavailable(self):
        from core.mlx_memory_gate import should_skip_second_mlx_checkpoint

        with patch("core.mlx_memory_gate.vm_pressure_level", return_value=None), \
             patch.dict("os.environ", {"KRAB_EAR_STT_SKIP_SECOND_MLX": ""}, clear=False):
            self.assertFalse(should_skip_second_mlx_checkpoint())

    def test_no_skip_when_pressure_normal(self):
        from core.mlx_memory_gate import should_skip_second_mlx_checkpoint

        with patch("core.mlx_memory_gate.vm_pressure_level", return_value=0), \
             patch.dict("os.environ", {"KRAB_EAR_STT_SKIP_SECOND_MLX": ""}, clear=False):
            self.assertFalse(should_skip_second_mlx_checkpoint())

    def test_env_force_skip(self):
        from core.mlx_memory_gate import should_skip_second_mlx_checkpoint

        with patch("core.mlx_memory_gate.vm_pressure_level", return_value=0), \
             patch.dict("os.environ", {"KRAB_EAR_STT_SKIP_SECOND_MLX": "1"}, clear=False):
            self.assertTrue(should_skip_second_mlx_checkpoint())


class MultipassSkipsSecondMlxUnderPressureTest(unittest.TestCase):
    def test_low_confidence_does_not_load_second_checkpoint(self):
        engine = _make_engine()
        first = _stt_result(0.42, model="mlx-community/whisper-large-v3-turbo")

        with patch.object(engine, "_transcribe_model") as mock_tm, \
             patch("core.engine.should_skip_second_mlx_checkpoint", return_value=True), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
            mock_cfg.model_max_list = [
                "mlx-community/whisper-large-v3-mlx",
                "mlx-community/whisper-large-v3-turbo",
            ]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            result = engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        mock_tm.assert_not_called()
        self.assertEqual(result["text"], "hello")
        skipped = [
            a for a in result.get("multipass_attempts", [])
            if a.get("skipped") == "vm_pressure"
        ]
        self.assertTrue(skipped, result.get("multipass_attempts"))

    def test_same_model_candidate_is_not_reloaded(self):
        engine = _make_engine()
        turbo = "mlx-community/whisper-large-v3-turbo"
        first = _stt_result(0.42, model=turbo)

        with patch.object(engine, "_transcribe_model") as mock_tm, \
             patch("core.engine.should_skip_second_mlx_checkpoint", return_value=False), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 2
            mock_cfg.MODEL_BALANCED = turbo
            mock_cfg.model_max_list = [turbo]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        mock_tm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
