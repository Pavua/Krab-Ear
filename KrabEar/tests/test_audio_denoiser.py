"""Тесты для core/audio_denoiser.py — адаптивное шумоподавление."""

from __future__ import annotations

import sys
import os
import unittest

import numpy as np

# Настройка PYTHONPATH для запуска как standalone
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.audio_denoiser import AudioDenoiser
from core.noise_profiler import NoiseProfiler

_SR = 16000  # стандартная частота дискретизации Whisper


def _make_clean_audio(duration_sec: float = 1.0, freq: float = 440.0) -> np.ndarray:
    """Синусоида — чистый тональный сигнал с высоким SNR."""
    t = np.linspace(0, duration_sec, int(_SR * duration_sec), endpoint=False)
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_noisy_audio(duration_sec: float = 1.0, snr_target_db: float = 5.0) -> np.ndarray:
    """Синусоида + белый шум с заданным SNR."""
    t = np.linspace(0, duration_sec, int(_SR * duration_sec), endpoint=False)
    signal = 0.5 * np.sin(2 * np.pi * 440.0 * t)
    signal_rms = float(np.sqrt(np.mean(signal ** 2)))
    noise_rms = signal_rms / (10 ** (snr_target_db / 20.0))
    rng = np.random.default_rng(42)
    noise = rng.standard_normal(len(t)) * noise_rms
    return np.clip(signal + noise, -1.0, 1.0).astype(np.float32)


class TestAudioDenoiserStrengthOff(unittest.TestCase):
    """strength='off' — всегда passthrough."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_strength_off_returns_identical_array(self) -> None:
        """При strength='off' массив не изменяется."""
        audio = _make_noisy_audio(snr_target_db=3.0)
        result = self.denoiser.denoise(audio, _SR, strength="off")
        np.testing.assert_array_equal(audio, result)

    def test_strength_off_noisy_audio_unchanged(self) -> None:
        """strength='off' даже для очень зашумлённого аудио — без обработки."""
        audio = _make_noisy_audio(snr_target_db=1.0)
        result = self.denoiser.denoise(audio, _SR, strength="off")
        self.assertEqual(audio.shape, result.shape)
        np.testing.assert_array_equal(audio, result)


class TestAudioDenoiserOutputShape(unittest.TestCase):
    """Форма и dtype выходного массива."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_output_shape_preserved(self) -> None:
        """Форма выходного массива совпадает с входной."""
        audio = _make_noisy_audio(duration_sec=0.5, snr_target_db=5.0)
        result = self.denoiser.denoise(audio, _SR, strength="moderate")
        self.assertEqual(audio.shape, result.shape)

    def test_output_shape_long_audio(self) -> None:
        """Форма сохраняется для длинного аудио."""
        audio = _make_noisy_audio(duration_sec=2.0, snr_target_db=8.0)
        result = self.denoiser.denoise(audio, _SR, strength="light")
        self.assertEqual(audio.shape, result.shape)

    def test_multichannel_input_produces_mono_output(self) -> None:
        """Многоканальный входной массив → моно на выходе."""
        mono = _make_noisy_audio(duration_sec=0.5, snr_target_db=5.0)
        stereo = np.stack([mono, mono], axis=1)  # (N, 2)
        result = self.denoiser.denoise(stereo, _SR, strength="moderate")
        # Деноизер усредняет до моно и возвращает 1-D
        self.assertEqual(result.ndim, 1)
        self.assertEqual(result.shape[0], stereo.shape[0])


class TestAudioDenoiserClipping(unittest.TestCase):
    """Значения выходного массива находятся в [-1, 1]."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_output_clipped_light(self) -> None:
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=4.0)
        result = self.denoiser.denoise(audio, _SR, strength="light")
        self.assertLessEqual(float(np.max(result)), 1.0)
        self.assertGreaterEqual(float(np.min(result)), -1.0)

    def test_output_clipped_strong(self) -> None:
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=2.0)
        result = self.denoiser.denoise(audio, _SR, strength="strong")
        self.assertLessEqual(float(np.max(result)), 1.0)
        self.assertGreaterEqual(float(np.min(result)), -1.0)


class TestAudioDenoiserSNRBasedDecision(unittest.TestCase):
    """Шумоподавление изменяет зашумлённое аудио, но оставляет чистое близким."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_noisy_audio_is_modified(self) -> None:
        """Зашумлённое аудио (SNR ~5 dB) изменяется деноизером."""
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=5.0)
        result = self.denoiser.denoise(audio, _SR, strength="moderate")
        # После обработки сигнал должен измениться
        diff = float(np.mean(np.abs(result.astype(np.float64) - audio.astype(np.float64))))
        self.assertGreater(diff, 1e-6, "Деноизер должен изменять зашумлённый сигнал")

    def test_noisy_audio_snr_improves(self) -> None:
        """После деноизинга шум уменьшается (RMS шума ниже)."""
        # Создаём сигнал с явным шумом
        t = np.linspace(0, 1.0, _SR, endpoint=False)
        signal = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float64)
        rng = np.random.default_rng(0)
        noise = rng.standard_normal(_SR) * 0.3
        audio = np.clip(signal + noise, -1.0, 1.0).astype(np.float32)

        result = self.denoiser.denoise(audio, _SR, strength="strong")

        # RMS результата должен быть меньше (шум подавлен)
        original_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        result_rms = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
        self.assertLess(result_rms, original_rms)


class TestNoiseProfilerSNRThreshold(unittest.TestCase):
    """NoiseProfiler: чистый сигнал даёт высокий SNR, зашумлённый — низкий."""

    def setUp(self) -> None:
        self.profiler = NoiseProfiler()

    def test_clean_audio_snr_above_threshold(self) -> None:
        """Чистый тональный сигнал → SNR выше стандартного порога 15 dB."""
        audio = _make_clean_audio(duration_sec=1.0)
        profile = self.profiler.profile(audio, _SR)
        self.assertGreater(profile.snr_db, 15.0)

    def test_white_noise_snr_below_threshold(self) -> None:
        """Белый шум без сигнала → SNR ниже 15 dB."""
        rng = np.random.default_rng(7)
        noise_only = (rng.standard_normal(_SR) * 0.4).astype(np.float32)
        profile = self.profiler.profile(noise_only, _SR)
        self.assertLess(profile.snr_db, 15.0)


if __name__ == "__main__":
    unittest.main()
