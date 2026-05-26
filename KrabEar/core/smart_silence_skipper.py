"""Умный пропуск тишины для аудиозаписей Krab Ear.

SmartSilenceSkipper удаляет длинные паузы (>1 с) из середины записи перед
передачей аудио в STT. Ведущие и завершающие 0.3 с сохраняются для контекста.
Вокруг каждого речевого сегмента добавляется отступ 0.1 с, чтобы не обрезать
начало/конец слов.

Настройка включается через SMART_SILENCE_SKIP_ENABLED (по умолчанию False).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from core.silence_detector import SilenceDetector, SILENCE_THRESHOLD_DB_PRESERVE_WHISPER

logger = logging.getLogger("KrabEar.SmartSilenceSkipper")

# --- Параметры пропуска тишины ---
_MIN_INTERNAL_SILENCE_SEC: float = 1.0   # минимальная длительность тишины для удаления
_EDGE_KEEP_SEC: float = 0.30             # сохраняем N секунд в начале и конце
_SPEECH_PAD_SEC: float = 0.10            # отступ вокруг каждого речевого сегмента
# Используем порог для сохранения шёпота: STT должен получить шёпотные
# фрагменты (-45…-55 дБ), а не терять их как «тишину».
_DEFAULT_THRESHOLD_DB: float = SILENCE_THRESHOLD_DB_PRESERVE_WHISPER


@dataclass
class SkipResult:
    """Результат обработки аудио SmartSilenceSkipper."""

    processed_audio: np.ndarray
    """Аудио с удалёнными тихими сегментами."""

    original_duration_sec: float
    """Длительность исходного аудио в секундах."""

    processed_duration_sec: float
    """Длительность обработанного аудио в секундах."""

    skipped_segments: list[dict] = field(default_factory=list)
    """Список удалённых сегментов: [{start, end, duration}, ...]."""

    time_saved_sec: float = 0.0
    """Сэкономленное время в секундах."""

    time_saved_pct: float = 0.0
    """Доля сэкономленного времени (0–100 %)."""


class SmartSilenceSkipper:
    """Удаляет длинные паузы из середины аудио перед STT.

    Логика:
    1. Сохраняет первые и последние _EDGE_KEEP_SEC секунд без изменений.
    2. В средней части находит тихие регионы длительностью > _MIN_INTERNAL_SILENCE_SEC.
    3. Для каждого такого региона вычитает отступы (_SPEECH_PAD_SEC) по краям
       (чтобы не обрезать слова), и удаляет оставшуюся тишину.
    4. Сшивает речевые куски в новый массив.
    """

    def __init__(
        self,
        threshold_db: float = _DEFAULT_THRESHOLD_DB,
        min_silence_sec: float = _MIN_INTERNAL_SILENCE_SEC,
        edge_keep_sec: float = _EDGE_KEEP_SEC,
        speech_pad_sec: float = _SPEECH_PAD_SEC,
    ) -> None:
        self._threshold_db = threshold_db
        self._min_silence_sec = min_silence_sec
        self._edge_keep_sec = edge_keep_sec
        self._speech_pad_sec = speech_pad_sec
        self._detector = SilenceDetector()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def process(self, audio: np.ndarray, sample_rate: int) -> SkipResult:
        """Обрабатывает аудио: удаляет длинные внутренние паузы.

        Args:
            audio: numpy-массив float32/float64, нормализованный в [-1, 1].
                   Многоканальные данные поддерживаются (используется первый канал).
            sample_rate: частота дискретизации в Гц.

        Returns:
            SkipResult с обработанным аудио и статистикой.
        """
        if sample_rate <= 0 or len(audio) == 0:
            return SkipResult(
                processed_audio=audio,
                original_duration_sec=0.0,
                processed_duration_sec=0.0,
            )

        original_duration = len(audio) / sample_rate

        # Работаем с моно для анализа, но сохраняем оригинальные каналы
        mono = SilenceDetector._to_mono(audio)

        # Все позиции в семплах
        edge_samples = int(self._edge_keep_sec * sample_rate)
        pad_samples = int(self._speech_pad_sec * sample_rate)
        min_silence_samples = int(self._min_silence_sec * sample_rate)

        n_samples = len(mono)

        # Если запись слишком короткая для анализа — возвращаем как есть
        if n_samples <= 2 * edge_samples:
            return SkipResult(
                processed_audio=audio,
                original_duration_sec=original_duration,
                processed_duration_sec=original_duration,
            )

        # Граница «средней» зоны (в семплах)
        inner_start = edge_samples
        inner_end = n_samples - edge_samples

        # Обнаруживаем тишину во всём аудио
        silence_regions = self._detector.detect_silence(
            mono, sample_rate, threshold_db=self._threshold_db
        )

        # Отбираем только те регионы, которые целиком лежат внутри средней зоны
        # и достаточно длинные
        skippable: list[tuple[int, int]] = []  # (start_sample, end_sample)
        for region in silence_regions:
            r_start = int(region.start_sec * sample_rate)
            r_end = int(region.end_sec * sample_rate)
            r_dur = r_end - r_start

            # Регион должен быть внутри средней зоны
            if r_start < inner_start or r_end > inner_end:
                continue

            # Достаточно ли длинная пауза?
            if r_dur < min_silence_samples:
                continue

            # Добавляем отступы по краям (не выходим за inner_start/inner_end)
            skip_start = min(r_start + pad_samples, r_end)
            skip_end = max(r_end - pad_samples, r_start)

            if skip_end <= skip_start:
                # После вычета отступов нечего удалять
                continue

            skippable.append((skip_start, skip_end))

        if not skippable:
            # Ничего не пропускаем
            return SkipResult(
                processed_audio=audio,
                original_duration_sec=original_duration,
                processed_duration_sec=original_duration,
            )

        # Объединяем пересекающиеся/смежные регионы
        skippable.sort(key=lambda x: x[0])
        merged: list[tuple[int, int]] = [skippable[0]]
        for s, e in skippable[1:]:
            if s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))

        # Строим список сохраняемых диапазонов
        keep_ranges: list[tuple[int, int]] = []
        prev = 0
        for skip_s, skip_e in merged:
            if prev < skip_s:
                keep_ranges.append((prev, skip_s))
            prev = skip_e
        if prev < n_samples:
            keep_ranges.append((prev, n_samples))

        # Собираем результирующее аудио
        chunks = [audio[s:e] for s, e in keep_ranges]
        processed = np.concatenate(chunks, axis=0) if len(chunks) > 1 else chunks[0].copy()

        processed_duration = len(processed) / sample_rate
        time_saved = original_duration - processed_duration
        time_saved_pct = (time_saved / original_duration * 100.0) if original_duration > 0 else 0.0

        # Формируем описание пропущенных сегментов
        skipped_segments = [
            {
                "start": round(s / sample_rate, 4),
                "end": round(e / sample_rate, 4),
                "duration": round((e - s) / sample_rate, 4),
            }
            for s, e in merged
        ]

        logger.debug(
            "SmartSilenceSkipper: удалено %d сегментов, сэкономлено %.2f с (%.1f %%)",
            len(skipped_segments),
            time_saved,
            time_saved_pct,
        )

        return SkipResult(
            processed_audio=processed,
            original_duration_sec=round(original_duration, 4),
            processed_duration_sec=round(processed_duration, 4),
            skipped_segments=skipped_segments,
            time_saved_sec=round(time_saved, 4),
            time_saved_pct=round(time_saved_pct, 2),
        )
