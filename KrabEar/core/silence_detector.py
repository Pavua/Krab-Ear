"""Детектор тишины для аудиозаписей Krab Ear.

SilenceDetector обнаруживает участки тишины, обрезает тишину с краёв
и вычисляет долю речи в записи.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from core.silence_constants import (  # W1333: shared threshold constants — source of truth
    SILENCE_THRESHOLD_AMP,
    SILENCE_THRESHOLD_DB,
    SILENCE_THRESHOLD_DB_STRICT,
    SILENCE_THRESHOLD_DB_PRESERVE_WHISPER,
)

logger = logging.getLogger("KrabEar.SilenceDetector")

# Размер фрейма для анализа тишины (в семплах)
_FRAME_SIZE = 512

# Re-export для обратной совместимости с модулями, которые импортируют из silence_detector.
# Источник истины — core.silence_constants.
__all__ = ["SilenceDetector", "SilenceRegion", "analyze_silence_file",
           "SILENCE_THRESHOLD_AMP", "SILENCE_THRESHOLD_DB",
           "SILENCE_THRESHOLD_DB_STRICT", "SILENCE_THRESHOLD_DB_PRESERVE_WHISPER"]


def _db_to_amplitude(db: float) -> float:
    """Конвертирует порог в дБ в амплитуду (RMS)."""
    return 10.0 ** (db / 20.0)


@dataclass
class SilenceRegion:
    """Участок тишины в аудиозаписи."""

    start_sec: float
    end_sec: float
    duration_sec: float

    def to_dict(self) -> dict:
        return {
            "start_sec": round(self.start_sec, 4),
            "end_sec": round(self.end_sec, 4),
            "duration_sec": round(self.duration_sec, 4),
        }


class SilenceDetector:
    """Детектор тишины: обнаружение, обрезка и оценка доли речи."""

    def detect_silence(
        self,
        audio: np.ndarray,
        sample_rate: int,
        threshold_db: float = SILENCE_THRESHOLD_DB,
    ) -> list[SilenceRegion]:
        """Обнаруживает участки тишины в аудио.

        Args:
            audio: numpy-массив float32/float64, нормализованный в [-1, 1].
                   Многоканальные данные усредняются в моно.
            sample_rate: частота дискретизации в Гц.
            threshold_db: порог тишины в дБ (по умолчанию -40 дБ).

        Returns:
            Список SilenceRegion с временными метками.
        """
        audio = self._to_mono(audio)
        if len(audio) == 0 or sample_rate <= 0:
            return []

        threshold_amp = _db_to_amplitude(threshold_db)
        n_samples = len(audio)
        n_frames = max(n_samples // _FRAME_SIZE, 1)
        frames = np.array_split(audio, n_frames)

        # Вычисляем RMS для каждого фрейма
        frame_rms = np.array([
            float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) if len(f) > 0 else 0.0
            for f in frames
        ])
        is_silent = frame_rms < threshold_amp

        # Группируем последовательные тихие фреймы в регионы
        regions: list[SilenceRegion] = []
        in_silence = False
        silence_start_frame = 0

        for i, silent in enumerate(is_silent):
            if silent and not in_silence:
                in_silence = True
                silence_start_frame = i
            elif not silent and in_silence:
                in_silence = False
                start_sec = silence_start_frame * _FRAME_SIZE / sample_rate
                end_sec = i * _FRAME_SIZE / sample_rate
                regions.append(SilenceRegion(
                    start_sec=start_sec,
                    end_sec=end_sec,
                    duration_sec=end_sec - start_sec,
                ))

        # Закрываем последний регион если запись заканчивается тишиной
        if in_silence:
            start_sec = silence_start_frame * _FRAME_SIZE / sample_rate
            end_sec = n_samples / sample_rate
            regions.append(SilenceRegion(
                start_sec=start_sec,
                end_sec=end_sec,
                duration_sec=end_sec - start_sec,
            ))

        return regions

    def trim_silence(
        self,
        audio: np.ndarray,
        sample_rate: int,
        threshold_db: float = SILENCE_THRESHOLD_DB,
        min_silence_sec: float = 0.5,
    ) -> np.ndarray:
        """Обрезает тишину в начале и конце аудио.

        Args:
            audio: numpy-массив float32/float64.
            sample_rate: частота дискретизации в Гц.
            threshold_db: порог тишины в дБ.
            min_silence_sec: минимальная длительность тишины для обрезки (сек).

        Returns:
            Аудио с обрезанной ведущей/завершающей тишиной.
        """
        mono = self._to_mono(audio)

        if len(mono) == 0 or sample_rate <= 0:
            return audio

        threshold_amp = _db_to_amplitude(threshold_db)
        min_silence_frames = int(min_silence_sec * sample_rate / _FRAME_SIZE)

        n_samples = len(mono)
        n_frames = max(n_samples // _FRAME_SIZE, 1)
        frames = np.array_split(mono, n_frames)

        frame_rms = np.array([
            float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) if len(f) > 0 else 0.0
            for f in frames
        ])
        is_silent = frame_rms < threshold_amp

        # Ищем первый и последний неtихий фрейм
        speech_frames = np.where(~is_silent)[0]
        if len(speech_frames) == 0:
            # Всё тихо — возвращаем пустой массив той же формы
            if audio.ndim > 1:
                return np.zeros((0, audio.shape[1]), dtype=audio.dtype)
            return np.zeros(0, dtype=audio.dtype)

        first_speech = int(speech_frames[0])
        last_speech = int(speech_frames[-1])

        # Проверяем минимальную длину тишины для обрезки
        leading_silence_frames = first_speech
        trailing_silence_frames = len(is_silent) - 1 - last_speech

        start_frame = first_speech if leading_silence_frames >= min_silence_frames else 0
        end_frame = last_speech + 1 if trailing_silence_frames >= min_silence_frames else len(is_silent)

        start_sample = start_frame * _FRAME_SIZE
        end_sample = min(end_frame * _FRAME_SIZE, n_samples)

        return audio[start_sample:end_sample]

    def get_speech_ratio(
        self,
        audio: np.ndarray,
        sample_rate: int,
        threshold_db: float = SILENCE_THRESHOLD_DB,
    ) -> float:
        """Возвращает долю речи от общей длительности (0-1).

        Args:
            audio: numpy-массив float32/float64.
            sample_rate: частота дискретизации в Гц.
            threshold_db: порог тишины в дБ.

        Returns:
            Доля речи: 0.0 = только тишина, 1.0 = только речь.
        """
        mono = self._to_mono(audio)
        if len(mono) == 0 or sample_rate <= 0:
            return 0.0

        threshold_amp = _db_to_amplitude(threshold_db)
        n_samples = len(mono)
        n_frames = max(n_samples // _FRAME_SIZE, 1)
        frames = np.array_split(mono, n_frames)

        frame_rms = np.array([
            float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) if len(f) > 0 else 0.0
            for f in frames
        ])
        speech_frames = int(np.sum(frame_rms >= threshold_amp))
        return speech_frames / len(frame_rms)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _to_mono(audio: np.ndarray) -> np.ndarray:
        """Конвертирует многоканальное аудио в моно."""
        if audio.ndim > 1:
            return audio.mean(axis=1).astype(audio.dtype)
        return audio


def analyze_silence_file(
    path: str | Path,
    threshold_db: float = SILENCE_THRESHOLD_DB,
    detector: Optional[SilenceDetector] = None,
) -> dict:
    """Анализирует тишину в аудиофайле по пути.

    Args:
        path: путь к аудиофайлу.
        threshold_db: порог тишины в дБ.
        detector: экземпляр SilenceDetector (создаётся если не передан).

    Returns:
        Словарь с результатами анализа тишины.
    """
    import soundfile as sf

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Аудиофайл не найден: {path}")

    audio_data, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if detector is None:
        detector = SilenceDetector()

    regions = detector.detect_silence(audio_data, sample_rate, threshold_db)
    speech_ratio = detector.get_speech_ratio(audio_data, sample_rate, threshold_db)
    duration_sec = len(audio_data) / max(sample_rate, 1)

    total_silence_sec = sum(r.duration_sec for r in regions)

    return {
        "file_path": str(path),
        "duration_sec": round(duration_sec, 4),
        "silence_regions": [r.to_dict() for r in regions],
        "silence_region_count": len(regions),
        "total_silence_sec": round(total_silence_sec, 4),
        "speech_ratio": round(speech_ratio, 4),
        "threshold_db": threshold_db,
    }
