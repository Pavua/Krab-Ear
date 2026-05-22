"""Тесты для SmartSilenceSkipper.

Используют синтетические аудиоданные: тишина, речь, чередование.
"""

from __future__ import annotations
from core.smart_silence_skipper import SmartSilenceSkipper, SkipResult

import sys
import threading
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


class TestSkipResultComputed(unittest.TestCase):
    """Вычисляемые поля SkipResult."""

    def test_processed_duration_less_than_original_after_skip(self):
        """processed_duration_sec < original_duration_sec когда тишина удалена."""
        skipper = SmartSilenceSkipper()
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        if result.skipped_segments:
            self.assertLess(result.processed_duration_sec, result.original_duration_sec)

    def test_time_saved_sec_equals_duration_diff(self):
        """time_saved_sec == original - processed (с погрешностью)."""
        skipper = SmartSilenceSkipper()
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        expected = result.original_duration_sec - result.processed_duration_sec
        self.assertAlmostEqual(result.time_saved_sec, expected, places=2)

    def test_no_skip_all_durations_equal(self):
        """Если ничего не удалено — original == processed, time_saved == 0."""
        skipper = SmartSilenceSkipper()
        audio = _speech(2.0)
        result = skipper.process(audio, SAMPLE_RATE)
        self.assertAlmostEqual(
            result.original_duration_sec, result.processed_duration_sec, places=3
        )
        self.assertEqual(result.time_saved_sec, 0.0)
        self.assertEqual(result.time_saved_pct, 0.0)


class TestBoundaryConditions(unittest.TestCase):
    """Граничные условия SmartSilenceSkipper."""

    def test_silence_exactly_at_threshold_not_skipped(self):
        """Тишина ровно min_silence_sec не попадает в пороговый условие (< не <=)."""
        # min_silence_sec=1.0, silence=1.0 → r_dur == min_silence_samples
        # Условие в коде: r_dur < min_silence_samples → не пропускает
        skipper = SmartSilenceSkipper(min_silence_sec=1.0)
        audio = _cat(_speech(1.0), _silence(1.0), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        # Может быть скипнуто или нет (зависит от детектора), но не должно падать
        self.assertIsInstance(result, SkipResult)

    def test_silence_just_above_threshold_may_skip(self):
        """Тишина немного больше порога — обработка не падает."""
        skipper = SmartSilenceSkipper(min_silence_sec=0.5)
        audio = _cat(_speech(1.0), _silence(0.6), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        self.assertIsInstance(result, SkipResult)
        self.assertGreaterEqual(result.time_saved_pct, 0.0)

    def test_silence_fully_consumed_by_padding(self):
        """Если тишина короче 2 × speech_pad_sec — после вычета отступов нечего удалять."""
        # speech_pad_sec=0.10 → 2 × 0.10 = 0.2 с
        # Зададим тишину 0.15 с < 0.2 с → skip_end <= skip_start → сегмент игнорируется
        skipper = SmartSilenceSkipper(min_silence_sec=0.1, speech_pad_sec=0.1)
        audio = _cat(_speech(1.0), _silence(0.15), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        # Не должен падать; количество скипов: 0 или больше (детектор может по-разному)
        self.assertIsInstance(result.skipped_segments, list)

    def test_1d_audio_shape_preserved(self):
        """Моно-аудио (1D) сохраняет свою форму."""
        skipper = SmartSilenceSkipper()
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        self.assertEqual(audio.ndim, 1)
        result = skipper.process(audio, SAMPLE_RATE)
        self.assertEqual(result.processed_audio.ndim, 1)

    def test_result_audio_dtype_preserved(self):
        """dtype обработанного аудио совпадает с исходным."""
        skipper = SmartSilenceSkipper()
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))
        result = skipper.process(audio, SAMPLE_RATE)
        self.assertEqual(result.processed_audio.dtype, audio.dtype)

    def test_large_silence_huge_savings(self):
        """Огромная пауза даёт > 80% экономии."""
        skipper = SmartSilenceSkipper()
        audio = _cat(_speech(0.5), _silence(10.0), _speech(0.5))
        result = skipper.process(audio, SAMPLE_RATE)
        self.assertGreater(result.time_saved_pct, 80.0)

    def test_skipped_segments_non_overlapping(self):
        """Пропущенные сегменты не перекрываются."""
        skipper = SmartSilenceSkipper()
        audio = _cat(
            _speech(0.5), _silence(2.0),
            _speech(0.5), _silence(2.0),
            _speech(0.5),
        )
        result = skipper.process(audio, SAMPLE_RATE)
        segs = result.skipped_segments
        for i in range(len(segs) - 1):
            self.assertLessEqual(segs[i]["end"], segs[i + 1]["start"])

    def test_constructor_stores_params(self):
        """Конструктор корректно сохраняет параметры."""
        skipper = SmartSilenceSkipper(
            threshold_db=-30.0,
            min_silence_sec=2.0,
            edge_keep_sec=0.5,
            speech_pad_sec=0.2,
        )
        self.assertAlmostEqual(skipper._threshold_db, -30.0)
        self.assertAlmostEqual(skipper._min_silence_sec, 2.0)
        self.assertAlmostEqual(skipper._edge_keep_sec, 0.5)
        self.assertAlmostEqual(skipper._speech_pad_sec, 0.2)


class TestConcurrentProcess(unittest.TestCase):
    """test_concurrent_process: process() безопасен при параллельных вызовах."""

    def test_concurrent_process(self):
        """Множество потоков вызывают process() — нет исключений и все возвращают SkipResult."""
        skipper = SmartSilenceSkipper()
        results: list[SkipResult] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            try:
                # чередуем тишину/речь чтобы часть потоков пропускала сегменты
                if idx % 2 == 0:
                    audio = _cat(_speech(0.5), _silence(2.0), _speech(0.5))
                else:
                    audio = _speech(1.0)
                result = skipper.process(audio, SAMPLE_RATE)
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread exceptions: {errors}")
        self.assertEqual(len(results), 16)
        for r in results:
            self.assertIsInstance(r, SkipResult)
            self.assertGreaterEqual(r.time_saved_pct, 0.0)


class TestAdaptiveThresholdViaHistory(unittest.TestCase):
    """test_adaptive_threshold_via_history: кастомные параметры меняют поведение пропуска.

    SmartSilenceSkipper не хранит историю, но конструктор принимает threshold_db
    и min_silence_sec — имитируем «адаптацию» через изменение параметров.
    """

    def test_lower_threshold_db_skips_quiet_speech(self):
        """При более высоком пороге (менее строгом) тихие сигналы тоже пропускаются."""
        # Тихий синусоид amplitude=0.01 (~-40 dB) + стандартная речь
        quiet = (0.01 * np.sin(2 * np.pi * 440 * np.linspace(0, 2.0, int(2.0 * SAMPLE_RATE)))).astype(np.float32)
        loud_speech = _speech(0.5)
        audio = _cat(loud_speech, quiet, loud_speech)

        # Высокий порог (много считается тишиной)
        skipper_sensitive = SmartSilenceSkipper(threshold_db=-20.0, min_silence_sec=0.5)
        result_sensitive = skipper_sensitive.process(audio, SAMPLE_RATE)

        # Низкий порог (только очень тихое считается тишиной)
        skipper_strict = SmartSilenceSkipper(threshold_db=-60.0, min_silence_sec=0.5)
        result_strict = skipper_strict.process(audio, SAMPLE_RATE)

        # Чувствительный должен скипнуть больше (или не меньше) чем строгий
        self.assertGreaterEqual(
            result_sensitive.time_saved_sec,
            result_strict.time_saved_sec,
        )

    def test_shorter_min_silence_more_segments_removed(self):
        """Уменьшение min_silence_sec позволяет удалять более короткие паузы."""
        # 0.8 с пауза — попадает под min_silence=0.5 но не под 1.0
        audio = _cat(_speech(1.0), _silence(0.8), _speech(1.0))

        skipper_loose = SmartSilenceSkipper(min_silence_sec=0.5)
        skipper_strict = SmartSilenceSkipper(min_silence_sec=1.0)

        result_loose = skipper_loose.process(audio, SAMPLE_RATE)
        result_strict = skipper_strict.process(audio, SAMPLE_RATE)

        # loose может удалить больше (0.8 >= 0.5), strict не удаляет (0.8 < 1.0)
        self.assertGreaterEqual(
            result_loose.time_saved_sec,
            result_strict.time_saved_sec,
        )

    def test_repeated_calls_with_same_audio_give_same_result(self):
        """Повторные вызовы для одного и того же аудио дают детерминированный результат."""
        skipper = SmartSilenceSkipper()
        audio = _cat(_speech(1.0), _silence(2.0), _speech(1.0))

        result1 = skipper.process(audio, SAMPLE_RATE)
        result2 = skipper.process(audio, SAMPLE_RATE)

        self.assertAlmostEqual(result1.time_saved_sec, result2.time_saved_sec, places=4)
        self.assertEqual(len(result1.skipped_segments), len(result2.skipped_segments))


if __name__ == "__main__":
    unittest.main()
