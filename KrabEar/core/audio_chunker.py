"""Умное разбиение длинных аудиозаписей на чанки для Krab Ear.

AudioChunker разбивает длинное аудио на сегменты, предпочитая разрезать
в точках тишины, а не на середине слова/фразы. Результаты транскрипции
нескольких чанков объединяются обратно в единый результат.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from core.silence_constants import SILENCE_THRESHOLD_DB
from core.silence_detector import SilenceDetector, SilenceRegion

logger = logging.getLogger("KrabEar.AudioChunker")

# Минимальный зазор тишины, который считается «естественной паузой»
_MIN_SILENCE_SEC = 0.3

# Отступ от края паузы (не режем в самом начале/конце тишины, а чуть внутри)
_SPLIT_OFFSET_SEC = 0.05


@dataclass
class AudioChunk:
    """Один аудиочанк с метаданными временных меток."""

    audio: np.ndarray
    """numpy-массив float32 аудиоданных чанка."""

    start_sec: float
    """Начало чанка в секундах относительно исходной записи."""

    end_sec: float
    """Конец чанка в секундах относительно исходной записи."""

    index: int
    """Порядковый номер чанка (0-based)."""

    def duration_sec(self) -> float:
        """Длительность чанка в секундах."""
        return self.end_sec - self.start_sec

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_sec": round(self.start_sec, 4),
            "end_sec": round(self.end_sec, 4),
            "duration_sec": round(self.duration_sec(), 4),
            "n_samples": len(self.audio),
        }


class AudioChunker:
    """Разбивает длинные аудиозаписи на чанки по паузам тишины.

    Алгоритм:
    1. Ищет регионы тишины через SilenceDetector.
    2. Выбирает точки разреза — ближайшую к границе max_chunk_sec паузу.
    3. Если подходящей паузы нет — режет жёстко по max_chunk_sec.

    Пример использования::

        chunker = AudioChunker()
        chunks = chunker.chunk(audio, sample_rate=16000, max_chunk_sec=30)
        # ... транскрибируем каждый чанк ...
        merged = AudioChunker.merge_results([
            {"text": "Привет", "start_sec": 0.0, "end_sec": 2.0},
            {"text": "мир", "start_sec": 2.0, "end_sec": 4.0},
        ])
    """

    def __init__(
        self,
        silence_detector: Optional[SilenceDetector] = None,
        threshold_db: float = SILENCE_THRESHOLD_DB,
        min_silence_sec: float = _MIN_SILENCE_SEC,
    ) -> None:
        """
        Args:
            silence_detector: экземпляр SilenceDetector (создаётся если не передан).
            threshold_db: порог тишины в дБ для детектора.
            min_silence_sec: минимальная длительность паузы, пригодной для разреза.
        """
        self._detector = silence_detector or SilenceDetector()
        self._threshold_db = threshold_db
        self._min_silence_sec = min_silence_sec

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def chunk(
        self,
        audio: np.ndarray,
        sample_rate: int,
        max_chunk_sec: float = 30.0,
    ) -> list[AudioChunk]:
        """Разбивает аудио на чанки не длиннее max_chunk_sec.

        Предпочитает точки тишины для разреза. Если в окне нет
        подходящей паузы — режет жёстко по max_chunk_sec.

        Args:
            audio: numpy-массив float32/float64, моно или стерео.
            sample_rate: частота дискретизации в Гц.
            max_chunk_sec: максимальная длительность одного чанка в секундах.

        Returns:
            Список AudioChunk (не менее одного), покрывающий всё аудио.

        Raises:
            ValueError: если sample_rate <= 0 или max_chunk_sec <= 0.
        """
        if sample_rate <= 0:
            raise ValueError(f"sample_rate должен быть > 0, получено: {sample_rate}")
        if max_chunk_sec <= 0:
            raise ValueError(f"max_chunk_sec должен быть > 0, получено: {max_chunk_sec}")

        # Нормализуем к моно для анализа пауз
        mono = SilenceDetector._to_mono(audio)
        total_samples = len(mono)
        total_sec = total_samples / sample_rate

        # Если запись короче max_chunk_sec — возвращаем один чанк
        if total_sec <= max_chunk_sec:
            logger.debug(
                "Аудио %.2f с <= %.2f с — один чанк.", total_sec, max_chunk_sec
            )
            chunk_audio = audio if audio.ndim == 1 else audio
            return [AudioChunk(audio=chunk_audio, start_sec=0.0, end_sec=total_sec, index=0)]

        # Обнаруживаем паузы
        silence_regions = self._detector.detect_silence(
            mono, sample_rate, threshold_db=self._threshold_db
        )
        # Отфильтровываем слишком короткие паузы
        usable_silences = [r for r in silence_regions if r.duration_sec >= self._min_silence_sec]

        logger.debug(
            "Аудио %.2f с: найдено %d пауз ≥ %.2f с.",
            total_sec,
            len(usable_silences),
            self._min_silence_sec,
        )

        # Вычисляем точки разреза
        split_points = self._compute_split_points(
            total_sec=total_sec,
            max_chunk_sec=max_chunk_sec,
            usable_silences=usable_silences,
        )

        # Нарезаем чанки
        return self._build_chunks(audio, mono, sample_rate, split_points, total_sec)

    @staticmethod
    def merge_results(chunks: list[dict]) -> dict:
        """Объединяет результаты транскрипции нескольких чанков.

        Ожидаемые поля каждого элемента:
            - ``text`` (str) — текст транскрипции (обязательно).
            - ``start_sec`` (float, опционально) — начало чанка.
            - ``end_sec`` (float, опционально) — конец чанка.
            - ``confidence`` (float, опционально) — уверенность модели.
            - ``language`` (str, опционально) — определённый язык.
            - ``segments`` (list[dict], опционально) — субсегменты Whisper.

        Returns:
            Объединённый результат::

                {
                    "text": "<весь текст через пробел>",
                    "start_sec": <мин>,
                    "end_sec": <макс>,
                    "confidence": <средняя>,
                    "language": <из первого чанка с language>,
                    "segments": [...все субсегменты...],
                    "chunk_count": <кол-во чанков>,
                }
        """
        if not chunks:
            return {
                "text": "",
                "start_sec": 0.0,
                "end_sec": 0.0,
                "confidence": 0.0,
                "language": None,
                "segments": [],
                "chunk_count": 0,
            }

        texts: list[str] = []
        all_segments: list[dict] = []
        confidences: list[float] = []
        language: Optional[str] = None
        start_sec: Optional[float] = None
        end_sec: Optional[float] = None

        for chunk in chunks:
            text = chunk.get("text", "")
            if text:
                texts.append(text.strip())

            if "confidence" in chunk and chunk["confidence"] is not None:
                confidences.append(float(chunk["confidence"]))

            if language is None and chunk.get("language"):
                language = chunk["language"]

            chunk_start = chunk.get("start_sec")
            chunk_end = chunk.get("end_sec")
            if chunk_start is not None:
                start_sec = min(start_sec, chunk_start) if start_sec is not None else chunk_start
            if chunk_end is not None:
                end_sec = max(end_sec, chunk_end) if end_sec is not None else chunk_end

            # Субсегменты Whisper — сдвигаем временны́е метки на start_sec чанка
            for seg in chunk.get("segments", []):
                adjusted = dict(seg)
                offset = chunk_start or 0.0
                if "start" in adjusted:
                    adjusted["start"] = adjusted["start"] + offset
                if "end" in adjusted:
                    adjusted["end"] = adjusted["end"] + offset
                all_segments.append(adjusted)

        merged_text = " ".join(t for t in texts if t)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        return {
            "text": merged_text,
            "start_sec": start_sec if start_sec is not None else 0.0,
            "end_sec": end_sec if end_sec is not None else 0.0,
            "confidence": round(avg_confidence, 4),
            "language": language,
            "segments": all_segments,
            "chunk_count": len(chunks),
        }

    # ------------------------------------------------------------------
    # Приватные методы
    # ------------------------------------------------------------------

    def _compute_split_points(
        self,
        total_sec: float,
        max_chunk_sec: float,
        usable_silences: list[SilenceRegion],
    ) -> list[float]:
        """Вычисляет список позиций разреза (в секундах).

        Алгоритм «жадного» разреза: начиная с позиции 0, двигаемся
        вперёд не более чем на max_chunk_sec. В этом окне ищем
        ближайшую к правой границе паузу. Если её нет — режем жёстко.

        Returns:
            Список точек разреза (без 0.0 и total_sec).
        """
        split_points: list[float] = []
        cursor = 0.0

        # Minimum advance per iteration to guarantee loop termination.
        # Chosen as half of max_chunk_sec so that even a silence right at
        # the start of a window still produces a meaningful chunk size and
        # the cursor always moves forward by > 0 (prevents micro-advance
        # regression where cursor += 0.01 s per step on leading silence).
        _MIN_ADVANCE_SEC = max_chunk_sec / 2.0

        while cursor + max_chunk_sec < total_sec:
            window_end = cursor + max_chunk_sec

            # Ищем паузу, середина которой попадает в (cursor, window_end]
            # Предпочитаем максимально позднюю паузу в окне
            best_cut: Optional[float] = None
            for region in usable_silences:
                # Skip silence regions that began before or exactly at cursor position
                if region.start_sec <= cursor:
                    continue
                mid = (region.start_sec + region.end_sec) / 2.0
                if cursor < mid <= window_end:
                    # Режем в начало паузы (с отступом от края).
                    # Гарантируем, что разрез продвигает курсор хотя бы
                    # на _MIN_ADVANCE_SEC, иначе откатываемся к жёсткому разрезу.
                    cut = region.start_sec + _SPLIT_OFFSET_SEC
                    if cut <= cursor + _MIN_ADVANCE_SEC:
                        cut = None  # type: ignore[assignment]
                    elif best_cut is None or cut > best_cut:
                        best_cut = cut

            if best_cut is not None:
                split_points.append(best_cut)
                cursor = best_cut
                logger.debug("Разрез по тишине: %.3f с", best_cut)
            else:
                # Нет подходящей паузы — жёсткий разрез
                split_points.append(window_end)
                cursor = window_end
                logger.debug("Жёсткий разрез: %.3f с", window_end)

        return split_points

    def _build_chunks(
        self,
        audio: np.ndarray,
        mono: np.ndarray,
        sample_rate: int,
        split_points: list[float],
        total_sec: float,
    ) -> list[AudioChunk]:
        """Нарезает массив audio по вычисленным точкам разреза."""
        boundaries = [0.0] + split_points + [total_sec]
        chunks: list[AudioChunk] = []

        for idx, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
            start_sample = int(round(start * sample_rate))
            end_sample = int(round(end * sample_rate))

            # Защита от выхода за пределы массива
            max_samples = len(audio) if audio.ndim == 1 else audio.shape[0]
            start_sample = max(0, min(start_sample, max_samples))
            end_sample = max(start_sample, min(end_sample, max_samples))

            chunk_audio = audio[start_sample:end_sample]
            chunks.append(
                AudioChunk(
                    audio=chunk_audio,
                    start_sec=start,
                    end_sec=end,
                    index=idx,
                )
            )

        return chunks
