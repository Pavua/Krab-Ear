"""Тесты AudioQualityAnalyzer — pre-flight анализ качества аудио."""

from __future__ import annotations
from core.audio_quality import AudioQualityAnalyzer, AudioQualityReport

import sys
import unittest
import math
from pathlib import Path

import numpy as np

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SR = 16000  # стандартная частота для тестов


def _sine(freq: float = 440.0, duration: float = 1.0, amplitude: float = 0.3, sr: int = SR) -> np.ndarray:
    """Синусоида заданной частоты и амплитуды."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _noise(duration: float = 1.0, amplitude: float = 0.01, sr: int = SR) -> np.ndarray:
    """Белый шум с заданной амплитудой."""
    rng = np.random.default_rng(42)
    return (amplitude * rng.standard_normal(int(sr * duration))).astype(np.float32)


def _silence(duration: float = 1.0, sr: int = SR) -> np.ndarray:
    """Полная тишина."""
    return np.zeros(int(sr * duration), dtype=np.float32)


class TestAudioQualityReportFields(unittest.TestCase):
    """Проверка наличия и типов полей AudioQualityReport."""

    def test_report_has_all_fields(self):
        analyzer = AudioQualityAnalyzer()
        audio = _sine() + _noise()
        report = analyzer.analyze(audio, SR)
        self.assertIsInstance(report, AudioQualityReport)
        self.assertIsInstance(report.rms_level, float)
        self.assertIsInstance(report.peak_level, float)
        self.assertIsInstance(report.snr_estimate_db, float)
        self.assertIsInstance(report.clipping_ratio, float)
        self.assertIsInstance(report.silence_ratio, float)
        self.assertIsInstance(report.duration_sec, float)
        self.assertIn(report.quality_score, ("excellent", "good", "fair", "poor"))
        self.assertIsInstance(report.warnings, list)

    def test_to_dict_keys(self):
        analyzer = AudioQualityAnalyzer()
        report = analyzer.analyze(_sine(), SR)
        d = report.to_dict()
        expected_keys = {
            "rms_level", "peak_level", "snr_estimate_db", "clipping_ratio",
            "silence_ratio", "duration_sec", "quality_score", "warnings",
        }
        self.assertEqual(set(d.keys()), expected_keys)


class TestRmsAndPeak(unittest.TestCase):
    """Проверка вычисления RMS и peak."""

    def test_rms_level_matches_known_value(self):
        # Синусоида амплитудой A имеет RMS = A/√2
        amplitude = 0.5
        audio = _sine(amplitude=amplitude, duration=2.0)
        analyzer = AudioQualityAnalyzer()
        report = analyzer.analyze(audio, SR)
        expected_rms = amplitude / math.sqrt(2)
        self.assertAlmostEqual(report.rms_level, expected_rms, delta=0.01)

    def test_peak_level_close_to_amplitude(self):
        amplitude = 0.7
        audio = _sine(amplitude=amplitude, duration=1.0)
        analyzer = AudioQualityAnalyzer()
        report = analyzer.analyze(audio, SR)
        self.assertAlmostEqual(report.peak_level, amplitude, delta=0.01)

    def test_silence_has_zero_rms_and_peak(self):
        analyzer = AudioQualityAnalyzer()
        report = analyzer.analyze(_silence(1.0), SR)
        self.assertAlmostEqual(report.rms_level, 0.0, delta=1e-9)
        self.assertAlmostEqual(report.peak_level, 0.0, delta=1e-9)


class TestClippingRatio(unittest.TestCase):
    """Проверка детектирования клиппинга."""

    def test_no_clipping_for_low_amplitude(self):
        audio = _sine(amplitude=0.3)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertAlmostEqual(report.clipping_ratio, 0.0, delta=1e-6)

    def test_clipping_detected_for_saturated_signal(self):
        # Все семплы = 1.0 → 100% клиппинг
        audio = np.ones(SR, dtype=np.float32)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertGreater(report.clipping_ratio, 0.99)

    def test_clipping_warning_present(self):
        audio = np.ones(SR * 2, dtype=np.float32)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertTrue(any("клиппинг" in w.lower() for w in report.warnings))


class TestSilenceRatio(unittest.TestCase):
    """Проверка доли тишины."""

    def test_full_silence_high_ratio(self):
        report = AudioQualityAnalyzer().analyze(_silence(2.0), SR)
        self.assertGreater(report.silence_ratio, 0.9)

    def test_active_signal_low_silence(self):
        audio = _sine(amplitude=0.3, duration=2.0)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertLess(report.silence_ratio, 0.1)

    def test_mixed_half_silence(self):
        active = _sine(amplitude=0.3, duration=1.0)
        silent = _silence(1.0)
        audio = np.concatenate([active, silent])
        report = AudioQualityAnalyzer().analyze(audio, SR)
        # Ожидаем ~50% тишины с допуском
        self.assertGreater(report.silence_ratio, 0.3)
        self.assertLess(report.silence_ratio, 0.7)


class TestQualityScore(unittest.TestCase):
    """Проверка итоговой оценки качества."""

    def test_excellent_for_clean_signal(self):
        # Сильный чистый сигнал: синусоида без шума → должен дать excellent или good
        audio = _sine(amplitude=0.5, duration=3.0)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertIn(report.quality_score, ("excellent", "good"))

    def test_poor_for_full_silence(self):
        report = AudioQualityAnalyzer().analyze(_silence(2.0), SR)
        self.assertEqual(report.quality_score, "poor")

    def test_poor_for_clipped_signal(self):
        audio = np.ones(SR * 2, dtype=np.float32)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertEqual(report.quality_score, "poor")

    def test_low_rms_signal_has_warning(self):
        # Очень слабый сигнал → предупреждение о низком уровне
        signal = _sine(amplitude=0.0005, duration=2.0)
        report = AudioQualityAnalyzer().analyze(signal, SR)
        self.assertTrue(any("уровень" in w.lower() for w in report.warnings))


class TestDuration(unittest.TestCase):
    """Проверка длительности."""

    def test_duration_correct(self):
        audio = _sine(duration=2.5)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertAlmostEqual(report.duration_sec, 2.5, delta=0.01)

    def test_short_audio_warning(self):
        # < 0.5с → предупреждение
        audio = _sine(duration=0.2)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertTrue(any("короткая" in w.lower() for w in report.warnings))


class TestMultichannel(unittest.TestCase):
    """Проверка работы с многоканальным аудио."""

    def test_stereo_is_mixed_to_mono(self):
        left = _sine(freq=440, amplitude=0.3, duration=1.0)
        right = _sine(freq=880, amplitude=0.3, duration=1.0)
        stereo = np.column_stack([left, right])  # (N, 2)
        report = AudioQualityAnalyzer().analyze(stereo, SR)
        # Должно обработаться без исключений
        self.assertGreater(report.duration_sec, 0.9)
        self.assertIn(report.quality_score, ("excellent", "good", "fair", "poor"))


class TestEdgeCases(unittest.TestCase):
    """Граничные случаи."""

    def test_empty_array_does_not_raise(self):
        report = AudioQualityAnalyzer().analyze(np.array([], dtype=np.float32), SR)
        self.assertEqual(report.rms_level, 0.0)
        self.assertEqual(report.peak_level, 0.0)

    def test_snr_positive_for_signal_over_noise(self):
        signal = _sine(amplitude=0.5, duration=2.0)
        noise = _noise(duration=2.0, amplitude=0.005)
        audio = signal + noise
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertGreater(report.snr_estimate_db, 10.0)


class TestClippingPartial(unittest.TestCase):
    """Частичный клиппинг и граничные случаи."""

    def test_partial_clipping_detected(self):
        """10% семплов насыщены → clipping_ratio ~0.1."""
        audio = _sine(amplitude=0.5, duration=2.0)
        clipped = audio.copy()
        # Принудительно насыщаем каждый 10-й семпл
        clipped[::10] = 1.0
        report = AudioQualityAnalyzer().analyze(clipped, SR)
        self.assertGreater(report.clipping_ratio, 0.0)

    def test_no_clipping_below_threshold(self):
        """Амплитуда 0.9 не достигает порога 0.99 → нет клиппинга."""
        audio = np.full(SR * 2, 0.9, dtype=np.float32)
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertAlmostEqual(report.clipping_ratio, 0.0, delta=1e-6)

    def test_clipping_ratio_in_range_0_to_1(self):
        """clipping_ratio всегда в [0, 1]."""
        for amp in [0.0, 0.5, 0.99, 1.0]:
            audio = np.full(SR, amp, dtype=np.float32)
            report = AudioQualityAnalyzer().analyze(audio, SR)
            self.assertGreaterEqual(report.clipping_ratio, 0.0)
            self.assertLessEqual(report.clipping_ratio, 1.0)


class TestSilenceRatioEdge(unittest.TestCase):
    """Доля тишины — граничные случаи."""

    def test_empty_audio_silence_ratio(self):
        """Пустой массив — silence_ratio может быть 1.0 (нет фреймов)."""
        report = AudioQualityAnalyzer().analyze(np.array([], dtype=np.float32), SR)
        # Не проверяем конкретное значение — лишь отсутствие исключения
        self.assertIsInstance(report.silence_ratio, float)

    def test_silence_warning_for_very_silent_audio(self):
        """Аудио с >80% тишины имеет предупреждение."""
        active = _sine(amplitude=0.3, duration=0.2)
        silent = _silence(duration=1.8)
        audio = np.concatenate([active, silent])
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertTrue(any("тишин" in w.lower() for w in report.warnings))


class TestQualityScoreEdge(unittest.TestCase):
    """Дополнительные тесты итоговой оценки."""

    def test_poor_score_for_mostly_silence(self):
        """Аудио >90% тишины → оценка poor."""
        active = _sine(amplitude=0.3, duration=0.1)
        silent = _silence(duration=2.9)
        audio = np.concatenate([active, silent])
        report = AudioQualityAnalyzer().analyze(audio, SR)
        self.assertEqual(report.quality_score, "poor")

    def test_quality_score_always_valid(self):
        """quality_score всегда один из допустимых."""
        valid = {"excellent", "good", "fair", "poor"}
        for amp in [0.0, 0.001, 0.3, 0.99, 1.0]:
            audio = np.full(SR, amp, dtype=np.float32)
            report = AudioQualityAnalyzer().analyze(audio, SR)
            self.assertIn(report.quality_score, valid)

    def test_duration_zero_sample_rate_no_crash(self):
        """sample_rate=1 (граничный) не вызывает деления на ноль."""
        audio = np.zeros(10, dtype=np.float32)
        report = AudioQualityAnalyzer().analyze(audio, sample_rate=1)
        self.assertIsInstance(report.duration_sec, float)


if __name__ == "__main__":
    unittest.main()
