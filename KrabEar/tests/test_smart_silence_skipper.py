"""Тесты для SmartSilenceSkipper.

Используют синтетические аудиоданные: тишина, речь, чередование.
"""

from __future__ import annotations
from core.smart_silence_skipper import SmartSilenceSkipper, SkipResult

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


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _silence(duration_sec: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Абсолютная тишина заданной длительности."""
    return np.zeros(int(duration_sec * sr), dtype=np.float32)


def _speech(duration_sec: float, amplitude: float = 0.5, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Синусоидальный сигнал — имитация речи."""
    n = int(duration_sec * sr)
    t = np.linspace(0, duration_sec, n, endpoint=False)
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _cat(*arrays: np.ndarray) -> np.ndarray:
    return np.concatenate(arrays)


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestSkipResultDataclass(unittest.TestCase):
    """Проверка структуры SkipResult."""

    def test_default_fields(self):
        """SkipResult создаётся с дефолтными значениями."""
        audio = np.zeros(100, dtype=np.float32)
        result = SkipResult(
            processed_audio=audio,
            original_duration_sec=1.0,
            processed_duration_sec=1.0,
        )
        self.assertEqual(result.skipped_segments, [])
        self.assertEqual(result.time_saved_sec, 0.0)
        self.assertEqual(result.time_saved_pct, 0.0)

    def test_fields_accessible(self):
        """Все поля SkipResult доступны по имени."""
        audio = _speech(0.5)
        result = SkipResult(
            processed_audio=audio,
            original_duration_sec=2.0,
            processed_duration_sec=0.5,
            skipped_segments=[{"start": 0.5, "end": 2.0, "duration": 1.5}],
            time_saved_sec=1.5,
            time_saved_pct=75.0,
        )
        self.assertAlmostEqual(result.time_saved_sec, 1.5)
        self.assertAlmostEqual(result.time_saved_pct, 75.0)
        self.assertEqual(len(result.skipped_segments), 1)


class TestShortAudio(unittest.TestCase):
    """Короткие записи не должны изменяться."""

    def setUp(self):
        self.skipper = SmartSilenceSkipper()

    def test_empty_audio_returns_as_is(self):
        """Пустой массив возвращается без изменений."""
        audio = np.array([], dtype=np.float32)
        result = self.skipper.process(audio, SAMPLE_RATE)
        self.assertEqual(len(result.processed_audio), 0)
        self.assertEqual(result.original_duration_sec, 0.0)
        self.assertEqual(result.skipped_segments, [])

    def test_zero_sample_rate_returns_as_is(self):
        """Нулевая частота дискретизации — возвращаем без изменений."""
        audio = _speech(2.0)
        result = self.skipper.process(audio, 0)
        np.testing.assert_array_equal(result.processed_audio, audio)

    def test_audio_shorter_than_two_edges_not_modified(self):
        """Запись короче 2 × edge_keep_sec возвращается как есть."""
        # edge_keep_sec = 0.3, значит 2 × 0.3 = 0.6 с — берём 0.5 с
        audio = _speech(0.5)
        result = self.skipper.process(audio, SAMPLE_RATE)
        np.testing.assert_array_equal(result.processed_audio, audio)
        self.assertEqual(result.skipped_segments, [])


class TestNoSilenceToSkip(unittest.TestCase):
    """Если нет длинных пауз — аудио не изменяется."""

    def setUp(self):
        self.skipper = SmartSilenceSkipper()

    def test_pure_speech_unchanged(self):
        """Сплошная речь возвращается без изменений."""
        audio = _speech(3.0)
        result = self.skipper.process(audio, SAMPLE_RATE)
        np.testing.assert_array_equal(result.processed_audio, audio)
        self.assertEqual(result.skipped_segments, [])
        self.assertEqual(result.time_saved_sec, 0.0)

    def test_short_internal_silence_not_skipped(self):
        """Паузы <1 с не удаляются."""
        # 1 с речи + 0.5 с тишина + 1 с речи
        audio = _cat(_speech(1.0), _silence(0.5), _speech(1.0))
        result = self.skipper.process(audio, SAMPLE_RATE)
        np.testing.assert_array_equal(result.processed_audio, audio)
        self.assertEqual(result.skipped_segments, [])


class TestSilenceSkipping(unittest.TestCase):
    """Основные сценарии удаления тишины."""

    def setUp(self):
        self.skipper = SmartSilenceSkipper()

    def test_single_long_silence_removed(self):
        """Одна длинная пауза в середине удаляется."""
        # 1 с речи + 2 с тишина + 1 с речи = 4 с
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = self.skipper.process(audio, SAMPLE_RATE)

        self.assertGreater(len(result.skipped_segments), 0)
        self.assertLess(result.processed_duration_sec, result.original_duration_sec)
        self.assertGreater(result.time_saved_sec, 0.0)
        self.assertGreater(result.time_saved_pct, 0.0)

    def test_processed_audio_length_matches_duration(self):
        """Длина processed_audio соответствует processed_duration_sec."""
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = self.skipper.process(audio, SAMPLE_RATE)
        expected_samples = int(result.processed_duration_sec * SAMPLE_RATE)
        # Допускаем погрешность ±2 фрейма (округление)
        self.assertAlmostEqual(
            len(result.processed_audio), expected_samples, delta=2 * 512
        )

    def test_multiple_silences_all_removed(self):
        """Несколько длинных пауз — все удаляются."""
        audio = _cat(
            _speech(0.5),
            _silence(1.5),
            _speech(0.5),
            _silence(1.5),
            _speech(0.5),
        )
        result = self.skipper.process(audio, SAMPLE_RATE)
        # Должно быть 2 скипнутых сегмента
        self.assertEqual(len(result.skipped_segments), 2)
        self.assertLess(result.processed_duration_sec, result.original_duration_sec)

    def test_time_saved_pct_between_0_and_100(self):
        """time_saved_pct всегда в диапазоне [0, 100]."""
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = self.skipper.process(audio, SAMPLE_RATE)
        self.assertGreaterEqual(result.time_saved_pct, 0.0)
        self.assertLessEqual(result.time_saved_pct, 100.0)

    def test_edge_silence_not_removed(self):
        """Тишина в начале/конце (в пределах edge_keep_sec) НЕ удаляется."""
        # Тишина только на краях, середина — речь
        audio = _cat(_silence(0.5), _speech(2.0), _silence(0.5))
        result = self.skipper.process(audio, SAMPLE_RATE)
        # Ни один сегмент не должен быть пропущен
        self.assertEqual(result.skipped_segments, [])

    def test_skipped_segment_dict_keys(self):
        """Каждый элемент skipped_segments содержит start, end, duration."""
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = self.skipper.process(audio, SAMPLE_RATE)
        for seg in result.skipped_segments:
            self.assertIn("start", seg)
            self.assertIn("end", seg)
            self.assertIn("duration", seg)
            self.assertGreater(seg["end"], seg["start"])
            self.assertAlmostEqual(
                seg["duration"], seg["end"] - seg["start"], places=3
            )

    def test_speech_padding_preserved(self):
        """После удаления тишины speech_pad_sec остаётся вокруг речи."""
        # speech_pad_sec = 0.1 с, значит вырезанный кусок начинается
        # как минимум через 0.1 с после начала тишины.
        speech_pad = 0.10
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = self.skipper.process(audio, SAMPLE_RATE)

        if result.skipped_segments:
            seg = result.skipped_segments[0]
            # Вырезанный старт должен быть >= 1.0 + speech_pad (с погрешностью фрейма)
            frame_sec = 512 / SAMPLE_RATE
            self.assertGreaterEqual(seg["start"], 1.0 + speech_pad - frame_sec)

    def test_multichannel_audio_processed(self):
        """Многоканальное аудио (stereo) обрабатывается без ошибок."""
        mono = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        stereo = np.stack([mono, mono], axis=1)  # shape (N, 2)
        result = self.skipper.process(stereo, SAMPLE_RATE)
        # Форма должна сохраниться: (M, 2)
        self.assertEqual(result.processed_audio.ndim, 2)
        self.assertEqual(result.processed_audio.shape[1], 2)
        self.assertLess(len(result.processed_audio), len(stereo))

    def test_original_duration_unchanged(self):
        """original_duration_sec отражает исходную длину, а не обработанную."""
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        expected = len(audio) / SAMPLE_RATE
        result = self.skipper.process(audio, SAMPLE_RATE)
        self.assertAlmostEqual(result.original_duration_sec, expected, places=3)

    def test_pure_silence_all_middle_removed(self):
        """Запись из краёв-речи и огромной паузы — пауза удаляется максимально."""
        # 0.5 с речи + 5 с тишина + 0.5 с речи
        audio = _cat(_speech(0.5), _silence(5.0), _speech(0.5))
        result = self.skipper.process(audio, SAMPLE_RATE)
        # Должно быть сэкономлено > 4 секунды
        self.assertGreater(result.time_saved_sec, 4.0)


class TestCustomParameters(unittest.TestCase):
    """Настраиваемые параметры SmartSilenceSkipper."""

    def test_custom_min_silence_respected(self):
        """min_silence_sec=0.5 — паузы от 0.5 с удаляются."""
        skipper = SmartSilenceSkipper(min_silence_sec=0.5)
        # 0.7 с тишина — должна удалиться при пороге 0.5 с
        audio = _cat(_speech(1.0), _silence(0.7), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        self.assertGreater(len(result.skipped_segments), 0)

    def test_high_threshold_means_no_skip(self):
        """Очень высокий порог минимальной тишины — ничего не удаляется."""
        skipper = SmartSilenceSkipper(min_silence_sec=10.0)
        # 2 с тишина < 10 с — не удаляется
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        self.assertEqual(result.skipped_segments, [])


if __name__ == "__main__":
    unittest.main()
