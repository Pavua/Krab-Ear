"""Тесты для SilenceDetector.

Используют синтетические аудиоданные: тишина, речь, смешанные сигналы.
"""

from __future__ import annotations
from core.silence_detector import SilenceDetector, SilenceRegion, _db_to_amplitude

import sys
import unittest
from pathlib import Path

import numpy as np

# Настройка путей для standalone запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


SAMPLE_RATE = 16000  # Гц


def _make_silence(duration_sec: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Создаёт массив нулей (абсолютная тишина)."""
    return np.zeros(int(duration_sec * sr), dtype=np.float32)


def _make_speech(duration_sec: float, amplitude: float = 0.5, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Создаёт синусоидальный сигнал (имитация речи)."""
    n = int(duration_sec * sr)
    t = np.linspace(0, duration_sec, n, endpoint=False)
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _concat(*arrays: np.ndarray) -> np.ndarray:
    return np.concatenate(arrays)


class TestDbToAmplitude(unittest.TestCase):
    """Тесты конвертации дБ → амплитуда."""

    def test_0db_is_1(self):
        self.assertAlmostEqual(_db_to_amplitude(0.0), 1.0, places=6)

    def test_minus20db(self):
        self.assertAlmostEqual(_db_to_amplitude(-20.0), 0.1, places=6)

    def test_minus40db(self):
        self.assertAlmostEqual(_db_to_amplitude(-40.0), 0.01, places=6)


class TestDetectSilence(unittest.TestCase):
    """Тесты метода detect_silence."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_pure_silence_returns_one_region(self):
        audio = _make_silence(2.0)
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0].duration_sec, 2.0, delta=0.1)

    def test_pure_speech_returns_no_regions(self):
        audio = _make_speech(2.0)
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(regions), 0)

    def test_speech_silence_speech_returns_one_region(self):
        audio = _concat(
            _make_speech(0.5),
            _make_silence(1.0),
            _make_speech(0.5),
        )
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(regions), 1)
        # Регион тишины ~1 сек
        self.assertAlmostEqual(regions[0].duration_sec, 1.0, delta=0.15)

    def test_leading_silence_detected(self):
        audio = _concat(_make_silence(0.8), _make_speech(1.0))
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertGreater(len(regions), 0)
        # Первый регион в начале
        self.assertLess(regions[0].start_sec, 0.2)

    def test_trailing_silence_detected(self):
        audio = _concat(_make_speech(1.0), _make_silence(0.8))
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertGreater(len(regions), 0)
        last = regions[-1]
        self.assertGreater(last.end_sec, 1.5)

    def test_empty_audio_returns_empty(self):
        regions = self.detector.detect_silence(np.zeros(0, dtype=np.float32), SAMPLE_RATE)
        self.assertEqual(regions, [])

    def test_silence_region_fields(self):
        audio = _concat(_make_speech(0.3), _make_silence(0.5), _make_speech(0.3))
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertGreater(len(regions), 0)
        r = regions[0]
        self.assertIsInstance(r, SilenceRegion)
        self.assertGreater(r.end_sec, r.start_sec)
        self.assertAlmostEqual(r.duration_sec, r.end_sec - r.start_sec, places=4)

    def test_multiple_silence_regions(self):
        audio = _concat(
            _make_speech(0.3),
            _make_silence(0.5),
            _make_speech(0.3),
            _make_silence(0.5),
            _make_speech(0.3),
        )
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(regions), 2)

    def test_to_dict(self):
        audio = _concat(_make_speech(0.5), _make_silence(0.5))
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertGreater(len(regions), 0)
        d = regions[-1].to_dict()
        self.assertIn("start_sec", d)
        self.assertIn("end_sec", d)
        self.assertIn("duration_sec", d)


class TestTrimSilence(unittest.TestCase):
    """Тесты метода trim_silence."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_trims_leading_silence(self):
        silence = _make_silence(1.0)
        speech = _make_speech(1.0)
        audio = _concat(silence, speech)
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE, min_silence_sec=0.3)
        # Обрезанное аудио должно быть короче оригинального
        self.assertLess(len(trimmed), len(audio))
        # Примерно равно длительности речевой части
        expected_samples = int(1.0 * SAMPLE_RATE)
        self.assertAlmostEqual(len(trimmed), expected_samples, delta=int(0.2 * SAMPLE_RATE))

    def test_trims_trailing_silence(self):
        speech = _make_speech(1.0)
        silence = _make_silence(1.0)
        audio = _concat(speech, silence)
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE, min_silence_sec=0.3)
        self.assertLess(len(trimmed), len(audio))

    def test_trims_both_ends(self):
        audio = _concat(_make_silence(0.5), _make_speech(1.0), _make_silence(0.5))
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE, min_silence_sec=0.3)
        # Должно быть близко к длине только речи
        expected = int(1.0 * SAMPLE_RATE)
        self.assertAlmostEqual(len(trimmed), expected, delta=int(0.3 * SAMPLE_RATE))

    def test_pure_silence_returns_empty(self):
        audio = _make_silence(2.0)
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(trimmed), 0)

    def test_no_trim_when_silence_too_short(self):
        # Короткая тишина меньше min_silence_sec — не обрезаем
        audio = _concat(_make_silence(0.1), _make_speech(1.0), _make_silence(0.1))
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE, min_silence_sec=0.5)
        # Длина должна быть равна или близка к исходной
        self.assertAlmostEqual(len(trimmed), len(audio), delta=int(0.2 * SAMPLE_RATE))

    def test_multichannel_audio(self):
        speech_mono = _make_speech(1.0)
        silence_mono = _make_silence(0.5)
        mono = _concat(silence_mono, speech_mono)
        stereo = np.stack([mono, mono], axis=1)
        trimmed = self.detector.trim_silence(stereo, SAMPLE_RATE, min_silence_sec=0.3)
        self.assertLess(len(trimmed), len(stereo))


class TestGetSpeechRatio(unittest.TestCase):
    """Тесты метода get_speech_ratio."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_pure_silence_ratio_is_zero(self):
        audio = _make_silence(2.0)
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertAlmostEqual(ratio, 0.0, delta=0.05)

    def test_pure_speech_ratio_is_one(self):
        audio = _make_speech(2.0)
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertAlmostEqual(ratio, 1.0, delta=0.05)

    def test_half_silence_half_speech(self):
        audio = _concat(_make_speech(1.0), _make_silence(1.0))
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertAlmostEqual(ratio, 0.5, delta=0.15)

    def test_ratio_in_range_0_to_1(self):
        audio = _concat(_make_silence(0.5), _make_speech(0.5), _make_silence(0.5))
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertGreaterEqual(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)

    def test_empty_audio_returns_zero(self):
        ratio = self.detector.get_speech_ratio(np.zeros(0, dtype=np.float32), SAMPLE_RATE)
        self.assertEqual(ratio, 0.0)

    def test_mostly_speech(self):
        # 10% тишина, 90% речь
        audio = _concat(_make_silence(0.1), _make_speech(0.9))
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertGreater(ratio, 0.7)

    def test_mostly_silence(self):
        # 10% речь, 90% тишина
        audio = _concat(_make_speech(0.1), _make_silence(0.9))
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertLess(ratio, 0.3)


class TestCustomThreshold(unittest.TestCase):
    """Тесты с нестандартным порогом тишины."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_higher_threshold_detects_quiet_speech_as_silence(self):
        # Тихий сигнал (-50 дБ)
        quiet_audio = _make_speech(1.0, amplitude=0.003)
        # С порогом -40 дБ он может быть тишиной
        ratio_strict = self.detector.get_speech_ratio(quiet_audio, SAMPLE_RATE, threshold_db=-40.0)
        # С порогом -60 дБ он — речь
        ratio_loose = self.detector.get_speech_ratio(quiet_audio, SAMPLE_RATE, threshold_db=-60.0)
        self.assertGreater(ratio_loose, ratio_strict)


class TestDetectSilenceEdgeCases(unittest.TestCase):
    """Дополнительные граничные тесты detect_silence."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_zero_sample_rate_returns_empty(self):
        """sample_rate=0 — безопасный возврат пустого списка."""
        audio = _make_silence(1.0)
        regions = self.detector.detect_silence(audio, sample_rate=0)
        self.assertEqual(regions, [])

    def test_all_loud_returns_no_regions(self):
        """Полностью громкий сигнал → нет регионов тишины."""
        audio = np.full(SAMPLE_RATE * 2, 0.8, dtype=np.float32)
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(regions), 0)

    def test_all_silent_long_returns_one_region(self):
        """Длинная тишина → один регион, покрывающий всё аудио."""
        audio = _make_silence(3.0)
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0].duration_sec, 3.0, delta=0.1)

    def test_multichannel_detect_silence(self):
        """Стерео аудио усредняется в моно перед анализом — нет исключений."""
        mono = _concat(_make_speech(0.5), _make_silence(1.0), _make_speech(0.5))
        stereo = np.stack([mono, mono], axis=1)
        regions = self.detector.detect_silence(stereo, SAMPLE_RATE)
        # Одна зона тишины в середине
        self.assertEqual(len(regions), 1)

    def test_silence_region_duration_consistency(self):
        """duration_sec == end_sec - start_sec для каждого региона."""
        audio = _concat(
            _make_speech(0.4),
            _make_silence(0.6),
            _make_speech(0.4),
            _make_silence(0.6),
        )
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        for r in regions:
            self.assertAlmostEqual(r.duration_sec, r.end_sec - r.start_sec, places=4)

    def test_silence_region_to_dict_complete(self):
        """to_dict() содержит все ожидаемые ключи и числовые значения."""
        audio = _concat(_make_speech(0.3), _make_silence(0.5))
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertGreater(len(regions), 0)
        d = regions[-1].to_dict()
        self.assertIn("start_sec", d)
        self.assertIn("end_sec", d)
        self.assertIn("duration_sec", d)
        self.assertIsInstance(d["start_sec"], float)
        self.assertIsInstance(d["end_sec"], float)
        self.assertIsInstance(d["duration_sec"], float)

    def test_silence_start_end_ordering(self):
        """start_sec < end_sec для каждого региона."""
        audio = _concat(
            _make_speech(0.3),
            _make_silence(0.5),
            _make_speech(0.3),
        )
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        for r in regions:
            self.assertLess(r.start_sec, r.end_sec)


class TestGetSpeechRatioEdge(unittest.TestCase):
    """Дополнительные граничные тесты get_speech_ratio."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_zero_sample_rate_returns_zero(self):
        audio = _make_speech(1.0)
        ratio = self.detector.get_speech_ratio(audio, sample_rate=0)
        self.assertEqual(ratio, 0.0)

    def test_all_zeros_returns_zero(self):
        audio = np.zeros(SAMPLE_RATE, dtype=np.float32)
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertAlmostEqual(ratio, 0.0, places=2)

    def test_all_loud_returns_one(self):
        audio = np.full(SAMPLE_RATE, 0.8, dtype=np.float32)
        ratio = self.detector.get_speech_ratio(audio, SAMPLE_RATE)
        self.assertAlmostEqual(ratio, 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
