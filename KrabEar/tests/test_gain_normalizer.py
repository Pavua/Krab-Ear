"""Тесты GainNormalizer — нормализация усиления аудио перед STT."""

from __future__ import annotations
from core.gain_normalizer import GainNormalizer, GainResult, _rms_db, _db_to_linear

import math
import sys
import unittest
from pathlib import Path

import numpy as np

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SR = 16000  # стандартная частота для тестов


# ---------------------------------------------------------------------------
# Вспомогательные генераторы сигналов
# ---------------------------------------------------------------------------

def _sine(
    freq: float = 440.0,
    duration: float = 1.0,
    amplitude: float = 0.1,
    sr: int = SR,
) -> np.ndarray:
    """Синусоида с заданной амплитудой."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _silence(duration: float = 1.0, sr: int = SR) -> np.ndarray:
    """Абсолютная тишина."""
    return np.zeros(int(sr * duration), dtype=np.float32)


def _loud(duration: float = 1.0, amplitude: float = 0.98, sr: int = SR) -> np.ndarray:
    """Громкий сигнал, близкий к клиппингу."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (amplitude * np.sin(2 * math.pi * 440 * t)).astype(np.float32)


def _stereo(duration: float = 1.0, amplitude: float = 0.1, sr: int = SR) -> np.ndarray:
    """Стерео сигнал (N, 2)."""
    mono = _sine(duration=duration, amplitude=amplitude, sr=sr)
    return np.stack([mono, mono * 0.8], axis=1)


# ---------------------------------------------------------------------------
# Тест 1: GainResult содержит правильные поля
# ---------------------------------------------------------------------------

class TestGainResultFields(unittest.TestCase):
    """Проверка типов и полей GainResult."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_result_is_gain_result_instance(self):
        result = self.normalizer.normalize(_sine())
        self.assertIsInstance(result, GainResult)

    def test_result_has_audio_array(self):
        result = self.normalizer.normalize(_sine())
        self.assertIsInstance(result.audio, np.ndarray)
        self.assertEqual(result.audio.dtype, np.float32)

    def test_result_numeric_fields_are_floats(self):
        result = self.normalizer.normalize(_sine())
        self.assertIsInstance(result.gain_applied_db, float)
        self.assertIsInstance(result.input_rms_db, float)
        self.assertIsInstance(result.output_rms_db, float)
        self.assertIsInstance(result.clipped_samples, int)

    def test_to_dict_has_expected_keys(self):
        result = self.normalizer.normalize(_sine())
        d = result.to_dict()
        self.assertIn("gain_applied_db", d)
        self.assertIn("input_rms_db", d)
        self.assertIn("output_rms_db", d)
        self.assertIn("clipped_samples", d)

    def test_audio_length_preserved(self):
        audio = _sine(duration=2.0)
        result = self.normalizer.normalize(audio)
        self.assertEqual(len(result.audio), len(audio))


# ---------------------------------------------------------------------------
# Тест 2: Нормализация к целевому RMS
# ---------------------------------------------------------------------------

class TestNormalizeTargetRms(unittest.TestCase):
    """Проверка, что выходной RMS соответствует target_db."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_rms_reaches_target_minus20(self):
        audio = _sine(amplitude=0.01, duration=2.0)  # тихий сигнал
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertAlmostEqual(result.output_rms_db, -20.0, delta=1.5)

    def test_rms_reaches_target_minus30(self):
        audio = _sine(amplitude=0.3, duration=2.0)
        result = self.normalizer.normalize(audio, target_db=-30.0)
        self.assertAlmostEqual(result.output_rms_db, -30.0, delta=1.5)

    def test_rms_reaches_target_minus10(self):
        audio = _sine(amplitude=0.01, duration=2.0)
        result = self.normalizer.normalize(audio, target_db=-10.0)
        # Мощный усилитель — ограничитель может скорректировать, но RMS должен быть близок
        self.assertGreater(result.output_rms_db, -15.0)

    def test_gain_applied_is_correct_sign(self):
        # Тихий сигнал → положительное усиление
        audio = _sine(amplitude=0.001, duration=1.0)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertGreater(result.gain_applied_db, 0.0)

    def test_loud_signal_gain_is_negative(self):
        # Громкий сигнал → отрицательное усиление (аттенюация)
        audio = _sine(amplitude=0.5, duration=1.0)
        result = self.normalizer.normalize(audio, target_db=-30.0)
        self.assertLess(result.gain_applied_db, 0.0)

    def test_input_rms_db_matches_actual(self):
        audio = _sine(amplitude=0.1, duration=2.0)
        expected_rms_db = _rms_db(audio)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertAlmostEqual(result.input_rms_db, expected_rms_db, delta=0.5)


# ---------------------------------------------------------------------------
# Тест 3: Тишина обрабатывается корректно
# ---------------------------------------------------------------------------

class TestSilenceHandling(unittest.TestCase):
    """Пустой / тихий сигнал не должен вызывать исключений."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_silence_does_not_raise(self):
        result = self.normalizer.normalize(_silence(2.0))
        self.assertIsInstance(result, GainResult)

    def test_silence_gain_is_zero(self):
        result = self.normalizer.normalize(_silence(1.0))
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)

    def test_empty_array_does_not_raise(self):
        result = self.normalizer.normalize(np.array([], dtype=np.float32))
        self.assertIsInstance(result, GainResult)
        self.assertEqual(len(result.audio), 0)

    def test_silence_clipped_samples_zero(self):
        result = self.normalizer.normalize(_silence(1.0))
        self.assertEqual(result.clipped_samples, 0)


# ---------------------------------------------------------------------------
# Тест 4: Soft-knee limiter не допускает клиппинга
# ---------------------------------------------------------------------------

class TestSoftKneeLimit(unittest.TestCase):
    """Проверка, что ограничитель не даёт амплитуде превысить 1.0."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_output_peak_does_not_exceed_1(self):
        # Чрезмерно громкий сигнал
        audio = np.ones(SR, dtype=np.float32) * 0.9
        result = self.normalizer.normalize(audio, target_db=-3.0)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)

    def test_very_loud_signal_peak_bounded(self):
        # Усиление тихого сигнала на 40 дБ может перегрузить выход
        audio = _sine(amplitude=0.001, duration=1.0)
        result = self.normalizer.normalize(audio, target_db=0.0)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)

    def test_limiter_counts_clipped_samples_for_saturated(self):
        # Полностью перегруженный сигнал — ограничитель должен зафиксировать много семплов
        audio = np.ones(SR * 2, dtype=np.float64) * 5.0  # сигнал 5×
        _, clipped = GainNormalizer._soft_knee_limit(audio.astype(np.float64))
        self.assertGreater(clipped, 0)

    def test_limiter_does_not_clip_low_signal(self):
        # Сигнал ниже порога ограничителя — клиппинга нет
        audio = _sine(amplitude=0.3, duration=1.0).astype(np.float64)
        limited, clipped = GainNormalizer._soft_knee_limit(audio)
        self.assertEqual(clipped, 0)
        np.testing.assert_array_almost_equal(limited, audio, decimal=5)


# ---------------------------------------------------------------------------
# Тест 5: auto_gain
# ---------------------------------------------------------------------------

class TestAutoGain(unittest.TestCase):
    """Проверка авто-нормализации."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_auto_gain_quiet_raises_rms(self):
        audio = _sine(amplitude=0.001, duration=2.0)
        result = self.normalizer.auto_gain(audio)
        self.assertGreater(result.output_rms_db, result.input_rms_db)

    def test_auto_gain_loud_lowers_peak(self):
        audio = _loud(amplitude=0.99, duration=1.0)
        result = self.normalizer.auto_gain(audio)
        peak_before = float(np.max(np.abs(audio)))
        peak_after = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak_after, peak_before + 1e-6)

    def test_auto_gain_output_peak_bounded(self):
        audio = _loud(amplitude=0.99, duration=1.0)
        result = self.normalizer.auto_gain(audio)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)

    def test_auto_gain_silence_unchanged(self):
        audio = _silence(1.0)
        result = self.normalizer.auto_gain(audio)
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)

    def test_auto_gain_returns_gain_result(self):
        result = self.normalizer.auto_gain(_sine())
        self.assertIsInstance(result, GainResult)


# ---------------------------------------------------------------------------
# Тест 6: Многоканальное аудио
# ---------------------------------------------------------------------------

class TestMultichannel(unittest.TestCase):
    """Стерео/многоканальное аудио усредняется в моно."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_stereo_normalize_does_not_raise(self):
        audio = _stereo(duration=1.0, amplitude=0.1)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertIsInstance(result, GainResult)

    def test_stereo_output_is_1d(self):
        audio = _stereo(duration=1.0, amplitude=0.1)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertEqual(result.audio.ndim, 1)

    def test_stereo_auto_gain_does_not_raise(self):
        audio = _stereo(duration=1.0, amplitude=0.05)
        result = self.normalizer.auto_gain(audio)
        self.assertIsInstance(result, GainResult)


# ---------------------------------------------------------------------------
# Тест 7: Корректность RMS-метрики и дБ-утилит
# ---------------------------------------------------------------------------

class TestRmsUtils(unittest.TestCase):
    """Проверка вспомогательных функций."""

    def test_rms_db_sine_known_value(self):
        # Синусоида амплитудой A: RMS = A/√2
        amplitude = 0.5
        audio = _sine(amplitude=amplitude, duration=2.0)
        expected_rms = amplitude / math.sqrt(2)
        expected_db = 20.0 * math.log10(expected_rms)
        actual_db = _rms_db(audio)
        self.assertAlmostEqual(actual_db, expected_db, delta=0.2)

    def test_db_to_linear_roundtrip(self):
        for db in [-40.0, -20.0, -6.0, 0.0, 6.0]:
            linear = _db_to_linear(db)
            recovered = 20.0 * math.log10(linear)
            self.assertAlmostEqual(recovered, db, delta=1e-6)

    def test_rms_db_silence_returns_floor(self):
        from core.gain_normalizer import _SILENCE_FLOOR_DB
        db = _rms_db(np.zeros(1000, dtype=np.float32))
        self.assertLessEqual(db, _SILENCE_FLOOR_DB)


# ---------------------------------------------------------------------------
# Тест 8: Идемпотентность повторной нормализации
# ---------------------------------------------------------------------------

class TestIdempotency(unittest.TestCase):
    """Повторная нормализация уже нормализованного сигнала не должна
    значительно менять уровень."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_second_normalize_is_stable(self):
        audio = _sine(amplitude=0.05, duration=2.0)
        first = self.normalizer.normalize(audio, target_db=-20.0)
        second = self.normalizer.normalize(first.audio, target_db=-20.0)
        # После второй нормализации RMS должен остаться близким к target
        self.assertAlmostEqual(second.output_rms_db, -20.0, delta=1.5)
        # Второй проход не должен сильно менять усиление
        self.assertAlmostEqual(second.gain_applied_db, 0.0, delta=2.0)

    def test_auto_gain_twice_peak_bounded(self):
        audio = _sine(amplitude=0.001, duration=2.0)
        first = self.normalizer.auto_gain(audio)
        second = self.normalizer.auto_gain(first.audio)
        peak = float(np.max(np.abs(second.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)


if __name__ == "__main__":
    unittest.main()
