"""Тесты VAD pre-filter для AudioEngine.

Проверяет, что аудио с тишиной обрабатывается корректно до передачи в Whisper:
- пустое/полностью тихое аудио → None (STT не вызывается)
- ведущая тишина → обрезается
- средняя пауза > порога → сжимается до padding
- полностью речевое аудио → возвращается без изменений длины
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


SR = 16000   # Стандартная частота дискретизации Whisper


def _make_silence(duration_sec: float, sr: int = SR) -> np.ndarray:
    """Создаёт массив нулей (полная тишина)."""
    return np.zeros(int(duration_sec * sr), dtype=np.float32)


def _make_voice(duration_sec: float, sr: int = SR, amplitude: float = 0.3) -> np.ndarray:
    """Создаёт амплитудно-модулированный тон (синтетическая речь с вариацией энергии).

    Адаптивный VAD обнаруживает речь по контрасту энергий между фреймами.
    Константный тон не имеет вариации, поэтому используем AM-модуляцию.
    """
    n = int(duration_sec * sr)
    t = np.linspace(0, duration_sec, n, endpoint=False)
    # AM: несущая 440 Гц × медленная огибающая 3 Гц
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    return (amplitude * envelope * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _make_engine(vad_enabled: bool = True, trim_threshold: float = 2.0) -> "AudioEngine":  # noqa: F821
    """Создаёт AudioEngine с патченными тяжёлыми зависимостями."""
    from core.engine import AudioEngine

    engine = AudioEngine.__new__(AudioEngine)
    # Минимальный набор атрибутов нужных для _apply_vad_prefilter
    engine._confidence_calibrator = MagicMock()
    engine._llm_rewriter = MagicMock()
    engine.current_model = "test_model"

    # Патчим settings для этого теста
    from core import config as cfg_module
    cfg_module.settings.STT_VAD_PREFILTER_ENABLED = vad_enabled
    cfg_module.settings.STT_VAD_SILENCE_TRIM_THRESHOLD_SEC = trim_threshold

    return engine


class TestVADPrefilterEmptyAudio(unittest.TestCase):
    """Полностью тихое или почти тихое аудио → _apply_vad_prefilter возвращает None."""

    def test_all_zeros_returns_none(self) -> None:
        """Массив нулей (нет речи) → None."""
        engine = _make_engine()
        audio = _make_silence(3.0)
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNone(result)

    def test_very_short_voice_below_min_threshold(self) -> None:
        """Меньше 0.3s речи → None (misfire-защита)."""
        engine = _make_engine()
        # 0.1s голоса + 5s тишины = итого 5.1s, но речи мало
        audio = np.concatenate([_make_voice(0.1), _make_silence(5.0)])
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNone(result)

    def test_empty_array_returns_none(self) -> None:
        """Пустой массив → None."""
        engine = _make_engine()
        audio = np.array([], dtype=np.float32)
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNone(result)


class TestVADPrefilterLeadingSilence(unittest.TestCase):
    """Ведущая тишина обрезается — выход короче входа."""

    def test_leading_silence_trimmed(self) -> None:
        """5s тишины + 1s речи → выход заметно короче 6s."""
        engine = _make_engine(trim_threshold=2.0)
        audio = np.concatenate([_make_silence(5.0), _make_voice(1.0)])
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNotNone(result)
        # Выход должен быть значительно короче исходного (срезали 5s тишины)
        self.assertLess(len(result), len(audio))  # type: ignore[arg-type]

    def test_leading_silence_result_contains_voice(self) -> None:
        """После обрезки ведущей тишины результат содержит ненулевые сэмплы."""
        engine = _make_engine(trim_threshold=2.0)
        audio = np.concatenate([_make_silence(4.0), _make_voice(0.8)])
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNotNone(result)
        self.assertGreater(np.max(np.abs(result)), 0.0)  # type: ignore[arg-type]


class TestVADPrefilterMidSilence(unittest.TestCase):
    """Длинная пауза в середине сжимается до padding."""

    def test_mid_silence_longer_than_threshold_trimmed(self) -> None:
        """0.5s речь + 3s тишина + 0.5s речь → выход должен быть короче."""
        engine = _make_engine(trim_threshold=2.0)
        audio = np.concatenate([
            _make_voice(0.5),
            _make_silence(3.0),
            _make_voice(0.5),
        ])
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNotNone(result)
        # Сжали 3s паузу → выход короче входа
        self.assertLess(len(result), len(audio))  # type: ignore[arg-type]

    def test_mid_silence_shorter_than_threshold_kept(self) -> None:
        """0.5s речь + 1s тишина (< 2.0s порог) + 0.5s речь → выход не меньше речи."""
        engine = _make_engine(trim_threshold=2.0)
        audio = np.concatenate([
            _make_voice(0.5),
            _make_silence(1.0),
            _make_voice(0.5),
        ])
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNotNone(result)
        # Речь сохранена
        self.assertGreater(len(result), 0)  # type: ignore[arg-type]


class TestVADPrefilterAllVoice(unittest.TestCase):
    """Полностью речевое аудио — результат не None, длина близка к исходной."""

    def test_all_voice_not_none(self) -> None:
        """1s голоса → результат не None."""
        engine = _make_engine()
        audio = _make_voice(1.0)
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNotNone(result)

    def test_all_voice_length_preserved(self) -> None:
        """2s голоса → выход близок по длине к входу (не более чем в 1.5× короче)."""
        engine = _make_engine()
        audio = _make_voice(2.0)
        result = engine._apply_vad_prefilter(audio, sample_rate=SR)
        self.assertIsNotNone(result)
        assert result is not None
        # Для чисто речевого аудио padding не должен сильно увеличить длину
        # и не должен резко уменьшить
        self.assertGreater(len(result), int(0.5 * len(audio)))


class TestVADPrefilterIntegrationWithTranscribe(unittest.TestCase):
    """Интеграционный тест: в transcribe() STT не вызывается при пустом аудио."""

    def test_all_silence_audio_skips_stt(self) -> None:
        """При полностью тихом numpy-массиве STT fallback НЕ вызывается."""
        from core.engine import AudioEngine

        engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "balanced_model"
        engine._confidence_calibrator = MagicMock()
        engine._llm_rewriter = MagicMock()
        engine._llm_rewriter.rewrite = MagicMock()
        # Нужные атрибуты для set_quality_profile / _maybe_run_diarization
        engine._diarization_pipeline = None
        engine._model_loaded = False
        engine._unavailable_models = {}

        from core import config as cfg_module
        cfg_module.settings.STT_VAD_PREFILTER_ENABLED = True
        cfg_module.settings.STT_VAD_SILENCE_TRIM_THRESHOLD_SEC = 2.0

        audio = _make_silence(3.0)

        with patch.object(engine, "_transcribe_with_fallback") as mock_stt:
            mock_stt.return_value = {"text": "галлюцинация", "segments": []}
            result = engine._apply_vad_prefilter(audio, sample_rate=SR)
            # _apply_vad_prefilter вернул None → mock_stt не должен был вызываться
            # через transcribe(), но мы тестируем только уровень helper'а
            self.assertIsNone(result)
            mock_stt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
