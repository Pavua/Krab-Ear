"""Тесты для AudioChunker.

Используют синтетические аудиоданные: тишина, речь, длинные записи.
Проверяют корректность разбиения на чанки и слияния результатов.
"""

from __future__ import annotations

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

from core.audio_chunker import AudioChunk, AudioChunker

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


def _total_sec(chunks: list[AudioChunk]) -> float:
    """Суммарная длительность чанков в секундах."""
    return sum(c.duration_sec() for c in chunks)


# ---------------------------------------------------------------------------
# 1. AudioChunk dataclass
# ---------------------------------------------------------------------------

class TestAudioChunkDataclass(unittest.TestCase):
    """Базовые свойства датакласса AudioChunk."""

    def _make_chunk(self, start: float = 0.0, end: float = 5.0, idx: int = 0) -> AudioChunk:
        audio = _speech(end - start)
        return AudioChunk(audio=audio, start_sec=start, end_sec=end, index=idx)

    def test_duration_sec(self):
        chunk = self._make_chunk(start=0.0, end=10.0)
        self.assertAlmostEqual(chunk.duration_sec(), 10.0, delta=0.01)

    def test_to_dict_keys(self):
        chunk = self._make_chunk(start=5.0, end=15.0, idx=2)
        d = chunk.to_dict()
        for key in ("index", "start_sec", "end_sec", "duration_sec", "n_samples"):
            self.assertIn(key, d)

    def test_to_dict_values(self):
        chunk = self._make_chunk(start=3.0, end=8.0, idx=1)
        d = chunk.to_dict()
        self.assertEqual(d["index"], 1)
        self.assertAlmostEqual(d["start_sec"], 3.0, delta=0.01)
        self.assertAlmostEqual(d["end_sec"], 8.0, delta=0.01)

    def test_audio_array_preserved(self):
        audio = _speech(2.0)
        chunk = AudioChunk(audio=audio, start_sec=0.0, end_sec=2.0, index=0)
        self.assertEqual(len(chunk.audio), len(audio))
        np.testing.assert_array_equal(chunk.audio, audio)


# ---------------------------------------------------------------------------
# 2. chunk() — короткое аудио (один чанк)
# ---------------------------------------------------------------------------

class TestChunkShortAudio(unittest.TestCase):
    """Аудио короче max_chunk_sec — должен возвращаться ровно один чанк."""

    def setUp(self):
        self.chunker = AudioChunker()

    def test_short_audio_returns_single_chunk(self):
        audio = _speech(10.0)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        self.assertEqual(len(chunks), 1)

    def test_single_chunk_covers_full_audio(self):
        audio = _speech(15.0)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        self.assertEqual(chunks[0].start_sec, 0.0)
        self.assertAlmostEqual(chunks[0].end_sec, 15.0, delta=0.1)

    def test_single_chunk_index_zero(self):
        audio = _speech(5.0)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        self.assertEqual(chunks[0].index, 0)

    def test_exactly_at_max_returns_one_chunk(self):
        # ровно 30 с не должно создавать второй чанк
        audio = _speech(30.0)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        self.assertEqual(len(chunks), 1)


# ---------------------------------------------------------------------------
# 3. chunk() — длинное аудио без пауз (жёсткий разрез)
# ---------------------------------------------------------------------------

class TestChunkHardSplit(unittest.TestCase):
    """Без пауз — жёсткий разрез по max_chunk_sec."""

    def setUp(self):
        self.chunker = AudioChunker()

    def test_long_pure_speech_splits_into_multiple(self):
        audio = _speech(65.0)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        self.assertGreater(len(chunks), 1)

    def test_chunks_cover_full_duration(self):
        audio = _speech(70.0)
        total_duration = len(audio) / SAMPLE_RATE
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        covered = sum(c.duration_sec() for c in chunks)
        self.assertAlmostEqual(covered, total_duration, delta=0.5)

    def test_chunk_indices_are_sequential(self):
        audio = _speech(90.0)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        indices = [c.index for c in chunks]
        self.assertEqual(indices, list(range(len(chunks))))

    def test_chunks_do_not_exceed_max_chunk_sec(self):
        audio = _speech(100.0)
        max_sec = 30.0
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=max_sec)
        for chunk in chunks[:-1]:  # последний чанк может быть короче
            self.assertLessEqual(chunk.duration_sec(), max_sec + 0.1)


# ---------------------------------------------------------------------------
# 4. chunk() — умный разрез по тишине
# ---------------------------------------------------------------------------

class TestChunkSmartSplit(unittest.TestCase):
    """Паузы обнаруживаются и используются как точки разреза."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)

    def _long_with_pauses(self) -> np.ndarray:
        """~65 с: 20 с речи + 1 с тишина + 20 с речи + 1 с тишина + 20 с речи."""
        return _cat(
            _speech(20.0),
            _silence(1.0),
            _speech(20.0),
            _silence(1.0),
            _speech(20.0),
        )

    def test_splits_at_silence_boundary(self):
        audio = self._long_with_pauses()
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        # Должно быть больше одного чанка
        self.assertGreater(len(chunks), 1)

    def test_silence_split_chunks_cover_full_duration(self):
        audio = self._long_with_pauses()
        total_sec = len(audio) / SAMPLE_RATE
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        covered = sum(c.duration_sec() for c in chunks)
        self.assertAlmostEqual(covered, total_sec, delta=1.0)

    def test_no_overlapping_chunks(self):
        audio = self._long_with_pauses()
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        for i in range(len(chunks) - 1):
            # Конец чанка i <= начало чанка i+1
            self.assertLessEqual(
                chunks[i].end_sec, chunks[i + 1].start_sec + 0.01,
                msg=f"Чанк {i} и {i+1} перекрываются",
            )

    def test_split_near_silence_not_mid_speech(self):
        """Разрез должен быть ближе к середине паузы (20-22 с), не к 30 с."""
        audio = self._long_with_pauses()
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        # Первый разрез должен быть в диапазоне 19-22 с (зона первой паузы)
        first_cut = chunks[0].end_sec
        self.assertGreater(first_cut, 18.0)
        self.assertLess(first_cut, 23.0)


# ---------------------------------------------------------------------------
# 5. chunk() — граничные случаи и ошибки
# ---------------------------------------------------------------------------

class TestChunkEdgeCases(unittest.TestCase):
    """Граничные условия и обработка ошибок."""

    def setUp(self):
        self.chunker = AudioChunker()

    def test_invalid_sample_rate_raises(self):
        audio = _speech(5.0)
        with self.assertRaises(ValueError):
            self.chunker.chunk(audio, sample_rate=0)

    def test_invalid_max_chunk_sec_raises(self):
        audio = _speech(5.0)
        with self.assertRaises(ValueError):
            self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=0.0)

    def test_stereo_audio_chunked(self):
        """Стерео аудио должно разбиваться без ошибок."""
        mono = _speech(65.0)
        stereo = np.stack([mono, mono], axis=1)
        chunks = self.chunker.chunk(stereo, SAMPLE_RATE, max_chunk_sec=30.0)
        self.assertGreater(len(chunks), 1)
        # Каждый чанк — 2D массив (стерео)
        for chunk in chunks:
            self.assertEqual(chunk.audio.ndim, 2)

    def test_empty_audio_returns_one_chunk(self):
        """Пустой массив — один «пустой» чанк."""
        audio = np.zeros(0, dtype=np.float32)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].audio), 0)

    def test_small_max_chunk_many_chunks(self):
        """Маленький max_chunk_sec — много чанков."""
        audio = _speech(60.0)
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=10.0)
        self.assertGreaterEqual(len(chunks), 5)


# ---------------------------------------------------------------------------
# 6. merge_results()
# ---------------------------------------------------------------------------

class TestMergeResults(unittest.TestCase):
    """Тесты метода merge_results."""

    def test_empty_list_returns_defaults(self):
        result = AudioChunker.merge_results([])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["chunk_count"], 0)
        self.assertEqual(result["segments"], [])

    def test_merges_text(self):
        chunks = [
            {"text": "Привет", "start_sec": 0.0, "end_sec": 2.0},
            {"text": "мир", "start_sec": 2.0, "end_sec": 4.0},
        ]
        result = AudioChunker.merge_results(chunks)
        self.assertEqual(result["text"], "Привет мир")

    def test_merges_multiple_texts(self):
        chunks = [
            {"text": "Один", "start_sec": 0.0, "end_sec": 5.0},
            {"text": "два", "start_sec": 5.0, "end_sec": 10.0},
            {"text": "три", "start_sec": 10.0, "end_sec": 15.0},
        ]
        result = AudioChunker.merge_results(chunks)
        self.assertEqual(result["text"], "Один два три")

    def test_start_end_sec(self):
        chunks = [
            {"text": "A", "start_sec": 0.0, "end_sec": 10.0},
            {"text": "B", "start_sec": 10.0, "end_sec": 25.0},
        ]
        result = AudioChunker.merge_results(chunks)
        self.assertAlmostEqual(result["start_sec"], 0.0, delta=0.01)
        self.assertAlmostEqual(result["end_sec"], 25.0, delta=0.01)

    def test_chunk_count(self):
        chunks = [{"text": "x"} for _ in range(5)]
        result = AudioChunker.merge_results(chunks)
        self.assertEqual(result["chunk_count"], 5)

    def test_average_confidence(self):
        chunks = [
            {"text": "A", "confidence": 0.8, "start_sec": 0.0, "end_sec": 5.0},
            {"text": "B", "confidence": 0.6, "start_sec": 5.0, "end_sec": 10.0},
        ]
        result = AudioChunker.merge_results(chunks)
        self.assertAlmostEqual(result["confidence"], 0.7, delta=0.01)

    def test_language_from_first_chunk(self):
        chunks = [
            {"text": "Привет", "language": "ru", "start_sec": 0.0, "end_sec": 5.0},
            {"text": "world", "language": "en", "start_sec": 5.0, "end_sec": 10.0},
        ]
        result = AudioChunker.merge_results(chunks)
        self.assertEqual(result["language"], "ru")

    def test_segments_time_offset(self):
        """Субсегменты должны получить смещение start_sec чанка."""
        chunks = [
            {
                "text": "первый",
                "start_sec": 0.0,
                "end_sec": 10.0,
                "segments": [{"start": 0.5, "end": 2.0, "text": "первый"}],
            },
            {
                "text": "второй",
                "start_sec": 10.0,
                "end_sec": 20.0,
                "segments": [{"start": 0.5, "end": 2.0, "text": "второй"}],
            },
        ]
        result = AudioChunker.merge_results(chunks)
        self.assertEqual(len(result["segments"]), 2)
        # Первый сегмент — без смещения (offset=0.0)
        self.assertAlmostEqual(result["segments"][0]["start"], 0.5, delta=0.01)
        # Второй сегмент — сдвинут на 10.0 с
        self.assertAlmostEqual(result["segments"][1]["start"], 10.5, delta=0.01)

    def test_missing_optional_fields(self):
        """Чанки без confidence/language/segments не вызывают ошибок."""
        chunks = [{"text": "просто текст"}]
        result = AudioChunker.merge_results(chunks)
        self.assertEqual(result["text"], "просто текст")
        self.assertEqual(result["segments"], [])
        self.assertIsNone(result["language"])

    def test_empty_text_chunks_skipped(self):
        """Чанки с пустым текстом не добавляют пробелов."""
        chunks = [
            {"text": "Привет", "start_sec": 0.0, "end_sec": 5.0},
            {"text": "", "start_sec": 5.0, "end_sec": 10.0},
            {"text": "мир", "start_sec": 10.0, "end_sec": 15.0},
        ]
        result = AudioChunker.merge_results(chunks)
        self.assertEqual(result["text"], "Привет мир")


# ---------------------------------------------------------------------------
# 7. Интеграция: chunk → merge
# ---------------------------------------------------------------------------

class TestChunkAndMergeIntegration(unittest.TestCase):
    """Полный цикл: разбиваем аудио → симулируем транскрипцию → сливаем."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)

    def test_chunk_count_matches_merge_chunk_count(self):
        audio = _cat(_speech(25.0), _silence(1.0), _speech(25.0))
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)

        fake_results = [
            {
                "text": f"текст_{i}",
                "start_sec": c.start_sec,
                "end_sec": c.end_sec,
                "confidence": 0.9,
            }
            for i, c in enumerate(chunks)
        ]
        merged = AudioChunker.merge_results(fake_results)
        self.assertEqual(merged["chunk_count"], len(chunks))

    def test_merged_end_sec_equals_audio_duration(self):
        audio = _cat(_speech(25.0), _silence(1.0), _speech(20.0))
        total_sec = len(audio) / SAMPLE_RATE
        chunks = self.chunker.chunk(audio, SAMPLE_RATE, max_chunk_sec=30.0)

        fake_results = [
            {"text": "x", "start_sec": c.start_sec, "end_sec": c.end_sec}
            for c in chunks
        ]
        merged = AudioChunker.merge_results(fake_results)
        self.assertAlmostEqual(merged["end_sec"], total_sec, delta=1.0)


if __name__ == "__main__":
    unittest.main()
