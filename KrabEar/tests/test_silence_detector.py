"""Тесты для SilenceDetector.

Используют синтетические аудиоданные: тишина, речь, смешанные сигналы.
"""

from __future__ import annotations
from core.silence_detector import (
    SilenceDetector,
    SilenceRegion,
    _db_to_amplitude,
    SILENCE_THRESHOLD_DB_STRICT,
    SILENCE_THRESHOLD_DB_PRESERVE_WHISPER,
)

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

    def test_trim_silence_2d_preserves_ndim_and_channels(self):
        """W1566 F5: trim_silence на 2D-массиве сохраняет ndim=2 и число каналов.

        Ранее одинаковые ветки (audio.ndim > 1 / else) обе возвращали audio[s:e],
        что корректно, но избыточно. После коллапса (F2) — один return. Тест
        проверяет, что поведение не изменилось: 2D-вход → 2D-выход с тем же
        количеством каналов.
        """
        n_channels = 3
        speech_mono = _make_speech(1.0)
        silence_mono = _make_silence(0.6)
        mono = _concat(silence_mono, speech_mono)
        multichannel = np.stack([mono] * n_channels, axis=1)  # shape (N, 3)
        self.assertEqual(multichannel.ndim, 2)

        trimmed = self.detector.trim_silence(multichannel, SAMPLE_RATE, min_silence_sec=0.3)

        self.assertEqual(trimmed.ndim, 2, "trim_silence должен сохранять ndim=2 для многоканального входа")
        self.assertEqual(trimmed.shape[1], n_channels, "число каналов должно оставаться неизменным")
        self.assertLess(len(trimmed), len(multichannel), "ведущая тишина должна быть обрезана")


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


class TestAlternatingSilenceSpeech(unittest.TestCase):
    """test_alternating_silence_speech — чередующиеся регионы."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_alternating_silence_speech(self):
        """Многократное чередование речи и тишины порождает соответствующее число регионов тишины."""
        segments = []
        for _ in range(4):
            segments.append(_make_speech(0.3))
            segments.append(_make_silence(0.4))
        audio = _concat(*segments)
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        # Должно быть 4 региона тишины (по одному после каждого речевого блока)
        self.assertEqual(len(regions), 4)

    def test_alternating_starts_with_silence(self):
        """Тишина в начале тоже считается отдельным регионом."""
        audio = _concat(
            _make_silence(0.3),
            _make_speech(0.3),
            _make_silence(0.3),
            _make_speech(0.3),
        )
        regions = self.detector.detect_silence(audio, SAMPLE_RATE)
        self.assertEqual(len(regions), 2)


class TestThresholdSensitivity(unittest.TestCase):
    """test_threshold_sensitivity — влияние порога тишины."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_low_threshold_treats_quiet_as_speech(self):
        """При очень низком пороге (-70 dB) тихий сигнал (-50 dB) считается речью."""
        quiet = _make_speech(1.0, amplitude=0.003)  # ~-50 dB RMS
        regions = self.detector.detect_silence(quiet, SAMPLE_RATE, threshold_db=-70.0)
        self.assertEqual(len(regions), 0, "очень низкий порог — речь не должна быть тишиной")

    def test_medium_threshold_default_behavior(self):
        """Средний порог (-40 dB): нормальная речь (0.5 амплитуды) — речь."""
        speech = _make_speech(1.0, amplitude=0.5)
        regions = self.detector.detect_silence(speech, SAMPLE_RATE, threshold_db=-40.0)
        self.assertEqual(len(regions), 0)

    def test_high_threshold_treats_speech_as_silence(self):
        """При очень высоком пороге (-10 dB) нормальная речь детектируется как тишина."""
        speech = _make_speech(1.0, amplitude=0.1)  # ~-20 dB RMS (ниже -10 dB порога)
        regions = self.detector.detect_silence(speech, SAMPLE_RATE, threshold_db=-10.0)
        self.assertGreater(len(regions), 0, "высокий порог — тихая речь становится тишиной")

    def test_threshold_increases_more_silence(self):
        """Повышение порога увеличивает суммарную длину тишины."""
        audio = _make_speech(2.0, amplitude=0.05)
        regions_low = self.detector.detect_silence(audio, SAMPLE_RATE, threshold_db=-60.0)
        regions_high = self.detector.detect_silence(audio, SAMPLE_RATE, threshold_db=-20.0)
        total_low = sum(r.duration_sec for r in regions_low)
        total_high = sum(r.duration_sec for r in regions_high)
        self.assertGreaterEqual(total_high, total_low)


class TestMinSilenceDurationFilter(unittest.TestCase):
    """test_min_silence_duration_filter — короткая тишина игнорируется при trim."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_short_silence_not_trimmed(self):
        """Очень короткая ведущая тишина (50 ms) < min_silence_sec (500 ms) — не обрезается."""
        tiny_silence = _make_silence(0.05)
        speech = _make_speech(1.0)
        audio = _concat(tiny_silence, speech)
        original_len = len(audio)
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE, min_silence_sec=0.5)
        self.assertAlmostEqual(len(trimmed), original_len, delta=int(0.1 * SAMPLE_RATE))

    def test_long_silence_is_trimmed(self):
        """Длинная ведущая тишина (1.0 s) > min_silence_sec (0.3 s) — обрезается."""
        long_silence = _make_silence(1.0)
        speech = _make_speech(1.0)
        audio = _concat(long_silence, speech)
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE, min_silence_sec=0.3)
        self.assertLess(len(trimmed), len(audio))

    def test_exact_boundary_min_silence(self):
        """Тишина ровно на границе min_silence_sec — обрезается."""
        border_silence = _make_silence(0.5)
        speech = _make_speech(1.0)
        audio = _concat(border_silence, speech)
        trimmed = self.detector.trim_silence(audio, SAMPLE_RATE, min_silence_sec=0.5)
        # Должно быть заметно короче, чем оригинал
        self.assertLess(len(trimmed), len(audio) - int(0.2 * SAMPLE_RATE))


class TestConcurrentDetect(unittest.TestCase):
    """test_concurrent_detect_thread_safe — потокобезопасность detect_silence."""

    def setUp(self):
        self.detector = SilenceDetector()

    def test_concurrent_detect_thread_safe(self):
        """Параллельный вызов detect_silence из нескольких потоков не вызывает ошибок."""
        import threading

        errors: list[Exception] = []
        results: list[list] = [[] for _ in range(10)]

        def run(idx: int):
            try:
                audio = _concat(_make_speech(0.3), _make_silence(0.4), _make_speech(0.3))
                regions = self.detector.detect_silence(audio, SAMPLE_RATE)
                results[idx] = regions
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Ошибки в потоках: {errors}")
        for i, res in enumerate(results):
            self.assertEqual(len(res), 1, f"Поток {i}: ожидался 1 регион тишины")


class TestThresholdConstants(unittest.TestCase):
    """W1016 F1+F2 — константы порогов тишины и сохранение шёпота (W1018)."""

    def test_strict_threshold_value(self):
        """SILENCE_THRESHOLD_DB_STRICT должен быть -40 дБ (для аналитики)."""
        self.assertEqual(SILENCE_THRESHOLD_DB_STRICT, -40.0)

    def test_preserve_whisper_threshold_value(self):
        """SILENCE_THRESHOLD_DB_PRESERVE_WHISPER должен быть -55 дБ (для STT-путей)."""
        self.assertEqual(SILENCE_THRESHOLD_DB_PRESERVE_WHISPER, -55.0)

    def test_preserve_whisper_lower_than_strict(self):
        """Порог сохранения шёпота должен быть ниже (дальше от 0) строгого порога."""
        self.assertLess(SILENCE_THRESHOLD_DB_PRESERVE_WHISPER, SILENCE_THRESHOLD_DB_STRICT)

    def test_whisper_at_minus50_db_not_classified_as_silence_in_preserve_mode(self):
        """Шёпот (~-50 дБ RMS) не должен классифицироваться как тишина при preserve-whisper пороге.

        W1016 F2: SmartSilenceSkipper + RealtimeSilenceFilter используют
        SILENCE_THRESHOLD_DB_PRESERVE_WHISPER (-55 дБ), поэтому шёпот (-50 дБ)
        классифицируется как речь — не как тишина.
        """
        detector = SilenceDetector()
        # Амплитуда 0.00316 ≈ -50 дБ RMS (типичный шёпот)
        whisper_audio = _make_speech(1.0, amplitude=0.00316)
        regions = detector.detect_silence(
            whisper_audio, SAMPLE_RATE,
            threshold_db=SILENCE_THRESHOLD_DB_PRESERVE_WHISPER,
        )
        self.assertEqual(
            len(regions), 0,
            "Шёпот (-50 дБ) не должен считаться тишиной при preserve-whisper пороге (-55 дБ)",
        )

    def test_strict_mode_still_classifies_whisper_as_silence(self):
        """Строгий (-40 дБ) порог классифицирует шёпот (-50 дБ) как тишину.

        W1016 F2: аналитический путь (get_speech_ratio, metrics) по-прежнему
        использует SILENCE_THRESHOLD_DB_STRICT, и для него шёпот = тишина.
        """
        detector = SilenceDetector()
        # Амплитуда 0.00316 ≈ -50 дБ RMS
        whisper_audio = _make_speech(1.0, amplitude=0.00316)
        regions = detector.detect_silence(
            whisper_audio, SAMPLE_RATE,
            threshold_db=SILENCE_THRESHOLD_DB_STRICT,
        )
        self.assertGreater(
            len(regions), 0,
            "Шёпот (-50 дБ) должен считаться тишиной при строгом пороге (-40 дБ)",
        )

    def test_trim_silence_dead_statement_removed(self):
        """F1: trim_silence не должен содержать мёртвый standalone audio.shape.

        Проверяем поведением: trim_silence корректно работает с 2D-массивом
        без исключений (раньше audio.shape возвращал tuple, но не использовался).
        """
        detector = SilenceDetector()
        mono = _concat(_make_silence(0.5), _make_speech(1.0))
        stereo = np.stack([mono, mono], axis=1)
        # Если мёртвый вызов audio.shape присутствовал — он всё равно
        # не вызывал ошибок, но убеждаемся что метод работает корректно.
        trimmed = detector.trim_silence(stereo, SAMPLE_RATE, min_silence_sec=0.3)
        self.assertLess(len(trimmed), len(stereo))
        self.assertEqual(trimmed.ndim, 2)


class TestSmartSilenceSkipperUsesPreserveWhisperThreshold(unittest.TestCase):
    """W1016 F2 — SmartSilenceSkipper использует SILENCE_THRESHOLD_DB_PRESERVE_WHISPER."""

    def test_default_threshold_is_preserve_whisper(self):
        """_DEFAULT_THRESHOLD_DB в smart_silence_skipper должен совпадать с PRESERVE_WHISPER."""
        from core.smart_silence_skipper import _DEFAULT_THRESHOLD_DB
        self.assertEqual(_DEFAULT_THRESHOLD_DB, SILENCE_THRESHOLD_DB_PRESERVE_WHISPER)

    def test_skipper_does_not_drop_whisper_segments(self):
        """SmartSilenceSkipper не удаляет шёпотные сегменты (−50 дБ) как тишину."""
        from core.smart_silence_skipper import SmartSilenceSkipper
        skipper = SmartSilenceSkipper()

        # Нормальная речь + шёпот + нормальная речь
        normal = _make_speech(1.0, amplitude=0.5)
        whisper = _make_speech(1.0, amplitude=0.00316)  # ~-50 дБ
        audio = _concat(normal, whisper, normal)

        result = skipper.process(audio, SAMPLE_RATE)
        # Шёпот не должен быть удалён — длина должна быть близка к оригиналу
        self.assertAlmostEqual(
            result.processed_duration_sec,
            result.original_duration_sec,
            delta=0.5,
            msg="SmartSilenceSkipper не должен удалять шёпотные участки",
        )


class TestRealtimeSilenceFilterUsesPreserveWhisperThreshold(unittest.TestCase):
    """W1016 F2 — RealtimeSilenceFilter использует SILENCE_THRESHOLD_DB_PRESERVE_WHISPER."""

    def test_default_threshold_is_preserve_whisper(self):
        """_DEFAULT_THRESHOLD_DB в realtime_silence_filter должен совпадать с PRESERVE_WHISPER."""
        from backend.realtime_silence_filter import _DEFAULT_THRESHOLD_DB
        self.assertEqual(_DEFAULT_THRESHOLD_DB, SILENCE_THRESHOLD_DB_PRESERVE_WHISPER)


class TestTwoTierThresholdExports(unittest.TestCase):
    """W1531 — проверяет наличие двухуровневых констант порогов тишины (регрессия W1018)."""

    def test_two_tier_thresholds_exported(self):
        """SILENCE_THRESHOLD_DB_STRICT и SILENCE_THRESHOLD_DB_PRESERVE_WHISPER
        должны быть экспортированы из core.silence_detector.

        W1525 мета-аудит выявил, что W1497 cherry-pick train откатил W1018 —
        оба имени отсутствовали в модуле, что приводило к ImportError и пустым
        транскриптам для тихой речи.
        """
        import core.silence_detector as sd
        self.assertTrue(
            hasattr(sd, "SILENCE_THRESHOLD_DB_STRICT"),
            "SILENCE_THRESHOLD_DB_STRICT должен быть экспортирован из core.silence_detector",
        )
        self.assertTrue(
            hasattr(sd, "SILENCE_THRESHOLD_DB_PRESERVE_WHISPER"),
            "SILENCE_THRESHOLD_DB_PRESERVE_WHISPER должен быть экспортирован из core.silence_detector",
        )

    def test_preserve_whisper_threshold_is_lower(self):
        """SILENCE_THRESHOLD_DB_PRESERVE_WHISPER должен быть ниже SILENCE_THRESHOLD_DB_STRICT.

        Более низкий порог (дальше от нуля в отрицательную сторону) означает,
        что тихая речь/шёпот не будет классифицирована как тишина.
        """
        self.assertLess(
            SILENCE_THRESHOLD_DB_PRESERVE_WHISPER,
            SILENCE_THRESHOLD_DB_STRICT,
            "PRESERVE_WHISPER (-55 дБ) должен быть меньше STRICT (-40 дБ)",
        )


if __name__ == "__main__":
    unittest.main()
