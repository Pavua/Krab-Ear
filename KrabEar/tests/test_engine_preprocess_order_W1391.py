"""W1391: Тесты порядка шагов preprocessing в AudioEngine.transcribe().

Проверяет, что AudioDenoiser выполняется ДО обнуления RSF-диапазонов
(silence_ranges), чтобы шумовой профиль строился по реальному ambient-шуму,
а не по нулям от RealtimeSilenceFilter.

Сценарии:
- test_engine_preprocess_order_denoiser_before_rsf: порядок вызовов
- test_engine_preprocess_idempotent_when_all_disabled: ничего не вызывается
- test_engine_preprocess_only_rsf_active: только RSF, без denoiser
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SR = 16000


def _make_audio(duration_sec: float = 2.0, sr: int = SR) -> np.ndarray:
    """Синтетическое аудио с ambient-шумом для тестов."""
    n = int(duration_sec * sr)
    rng = np.random.default_rng(42)
    return rng.standard_normal(n).astype(np.float32) * 0.05


def _build_engine() -> "AudioEngine":  # noqa: F821
    """Создаёт минимально-инициализированный AudioEngine без тяжёлых зависимостей."""
    from core.engine import AudioEngine

    engine = AudioEngine.__new__(AudioEngine)
    engine._confidence_calibrator = MagicMock()
    engine._llm_rewriter = MagicMock()
    engine.current_model = "balanced"
    engine._unavailable_models = {}
    engine._metrics_collector = MagicMock()
    engine._error_bus = MagicMock()
    return engine


class PreprocessOrderTestCase(unittest.TestCase):
    """Проверяет порядок шагов preprocessing в AudioEngine.transcribe()."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _patch_transcribe_with_fallback(self, engine: "AudioEngine", result: dict) -> MagicMock:  # noqa: F821
        """Патчит _transcribe_with_fallback чтобы не вызывать Whisper."""
        m = MagicMock(return_value=result)
        engine._transcribe_with_fallback = m
        return m

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_engine_preprocess_order_denoiser_before_rsf(self) -> None:
        """Denoiser должен вызываться ДО zero_silence_ranges (RSF zeroing).

        Если порядок неверный (RSF → Denoiser), шумовой профиль строится
        по нулям → noise floor underestimated → slient на strong-mode.
        """
        engine = _build_engine()

        audio = _make_audio(3.0)
        denoised_audio = audio * 0.9   # Denoiser чуть меняет сигнал
        zeroed_audio = denoised_audio.copy()
        zeroed_audio[:SR] = 0.0        # RSF обнуляет первую секунду

        call_order: list[str] = []

        def fake_maybe_denoise(a: np.ndarray) -> np.ndarray:
            call_order.append("denoiser")
            # Проверяем: на входе НЕ должно быть нулей от RSF (первая секунда ненулевая)
            self.assertGreater(
                float(np.abs(a[:SR]).max()), 1e-6,
                "Denoiser получил нулевой первый блок — RSF применён РАНЬШЕ!",
            )
            return denoised_audio

        def fake_zero_sr(a: np.ndarray, ranges, sample_rate: int = SR) -> np.ndarray:
            call_order.append("rsf")
            return zeroed_audio

        dummy_result = {
            "text": "тест", "segments": [], "confidence": 0.9,
        }
        self._patch_transcribe_with_fallback(engine, dummy_result)

        from core import config as cfg
        cfg.settings.STT_DENOISE_ENABLED = True
        cfg.settings.STT_VAD_PREFILTER_ENABLED = False
        cfg.settings.STT_MULTIPASS_ENABLED = False

        silence_ranges = [(0.0, 1.0)]  # одна секунда тишины

        with (
            patch.object(engine, "_maybe_denoise", side_effect=fake_maybe_denoise),
            patch(
                "backend.realtime_silence_filter.zero_silence_ranges",
                side_effect=fake_zero_sr,
            ),
        ):
            engine.transcribe(
                audio,
                lang_hint="ru",
                silence_ranges=silence_ranges,
                is_preview=False,
                diarize=False,
            )

        self.assertEqual(
            call_order,
            ["denoiser", "rsf"],
            f"Ожидался порядок denoiser→rsf, получен: {call_order}",
        )

    def test_engine_preprocess_idempotent_when_all_disabled(self) -> None:
        """Если и denoiser и RSF отключены — ни один не вызывается."""
        engine = _build_engine()

        audio = _make_audio(2.0)
        dummy_result = {"text": "тест", "segments": [], "confidence": 0.9}
        self._patch_transcribe_with_fallback(engine, dummy_result)

        from core import config as cfg
        cfg.settings.STT_DENOISE_ENABLED = False
        cfg.settings.STT_VAD_PREFILTER_ENABLED = False
        cfg.settings.STT_MULTIPASS_ENABLED = False

        mock_denoise = MagicMock()
        mock_rsf = MagicMock()

        with (
            patch.object(engine, "_maybe_denoise", mock_denoise),
            patch(
                "backend.realtime_silence_filter.zero_silence_ranges",
                mock_rsf,
            ),
        ):
            engine.transcribe(
                audio,
                lang_hint="ru",
                silence_ranges=None,
                is_preview=False,
                diarize=False,
            )

        mock_denoise.assert_not_called()
        mock_rsf.assert_not_called()

    def test_engine_preprocess_only_rsf_active(self) -> None:
        """Только RSF активен (denoiser отключён): RSF вызывается, denoiser нет."""
        engine = _build_engine()

        audio = _make_audio(2.0)
        zeroed = audio.copy()
        zeroed[:SR] = 0.0

        dummy_result = {"text": "тест", "segments": [], "confidence": 0.9}
        self._patch_transcribe_with_fallback(engine, dummy_result)

        from core import config as cfg
        cfg.settings.STT_DENOISE_ENABLED = False
        cfg.settings.STT_VAD_PREFILTER_ENABLED = False
        cfg.settings.STT_MULTIPASS_ENABLED = False

        mock_denoise = MagicMock()
        rsf_called_with_audio: list[np.ndarray] = []

        def fake_zero_sr(a: np.ndarray, ranges, sample_rate: int = SR) -> np.ndarray:
            rsf_called_with_audio.append(a.copy())
            return zeroed

        with (
            patch.object(engine, "_maybe_denoise", mock_denoise),
            patch(
                "backend.realtime_silence_filter.zero_silence_ranges",
                side_effect=fake_zero_sr,
            ),
        ):
            engine.transcribe(
                audio,
                lang_hint="ru",
                silence_ranges=[(0.0, 1.0)],
                is_preview=False,
                diarize=False,
            )

        mock_denoise.assert_not_called()
        self.assertEqual(len(rsf_called_with_audio), 1, "RSF должен вызываться один раз")
        # RSF получил исходный audio (не изменённый denoiser'ом, т.к. он отключён)
        np.testing.assert_array_equal(
            rsf_called_with_audio[0], audio,
            err_msg="RSF получил не исходный audio",
        )


if __name__ == "__main__":
    unittest.main()
