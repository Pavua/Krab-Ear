"""Нормализация усиления аудио перед STT.

GainNormalizer приводит входной сигнал к целевому уровню RMS (в дБFS),
применяет мягкое ограничение пиков (soft-knee limiter) для предотвращения
клиппинга и возвращает подробный GainResult с диагностикой.

Только numpy — без внешних зависимостей.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger("KrabEar.GainNormalizer")

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

_SILENCE_FLOOR_DB: float = -80.0   # ниже этого — считаем сигнал тишиной
_DEFAULT_TARGET_DB: float = -20.0  # целевой уровень RMS для normalize()
_LIMITER_THRESHOLD: float = 0.95   # порог пика, выше которого включается ограничитель
_LIMITER_KNEE_DB: float = 6.0      # ширина мягкого колена в дБ
_HARD_CLIP: float = 1.0            # абсолютный максимум амплитуды после ограничителя


# ---------------------------------------------------------------------------
# Результирующий датакласс
# ---------------------------------------------------------------------------

@dataclass
class GainResult:
    """Результат нормализации усиления аудио."""

    audio: np.ndarray          # обработанный аудиомассив (float32 [-1, 1])
    gain_applied_db: float     # применённое усиление в дБ (положит. = усиление)
    input_rms_db: float        # уровень RMS входного сигнала в дБFS
    output_rms_db: float       # уровень RMS выходного сигнала в дБFS
    clipped_samples: int       # число семплов, обрезанных ограничителем

    def to_dict(self) -> dict:
        """Сериализует диагностику в словарь (без массива аудио)."""
        return {
            "gain_applied_db": self.gain_applied_db,
            "input_rms_db": self.input_rms_db,
            "output_rms_db": self.output_rms_db,
            "clipped_samples": self.clipped_samples,
        }


# ---------------------------------------------------------------------------
# Утилитарные функции
# ---------------------------------------------------------------------------

def _rms_db(audio: np.ndarray) -> float:
    """Вычисляет уровень RMS сигнала в дБFS."""
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < 1e-12:
        return _SILENCE_FLOOR_DB
    return float(20.0 * math.log10(rms))


def _db_to_linear(db: float) -> float:
    """Конвертирует дБ в линейный коэффициент усиления."""
    return float(10.0 ** (db / 20.0))


def _linear_to_db(linear: float) -> float:
    """Конвертирует линейный коэффициент в дБ."""
    if linear <= 0.0:
        return _SILENCE_FLOOR_DB
    return float(20.0 * math.log10(linear))


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class GainNormalizer:
    """Нормализатор усиления аудио.

    Алгоритм:
    1. Вычисляет RMS входного сигнала.
    2. Вычисляет коэффициент усиления для достижения target_db.
    3. Применяет усиление.
    4. Обрабатывает результат soft-knee limiter'ом для предотвращения
       клиппинга выше _LIMITER_THRESHOLD.
    5. Возвращает GainResult с диагностикой.

    Пустой сигнал или тишина (RMS < -80 дБFS) возвращается без изменений.
    """

    def normalize(
        self,
        audio: np.ndarray,
        target_db: float = _DEFAULT_TARGET_DB,
    ) -> GainResult:
        """Нормализует аудио к заданному целевому уровню RMS.

        Args:
            audio: numpy-массив float32/float64 в диапазоне [-1, 1].
                   Многоканальное аудио усредняется в моно автоматически.
            target_db: желаемый уровень RMS выхода в дБFS (по умолчанию -20.0).

        Returns:
            GainResult с нормализованным аудио и диагностикой.
        """
        audio = self._to_mono_float32(audio)

        input_rms_db = _rms_db(audio)

        # Тихий сигнал — возвращаем без изменений
        if input_rms_db <= _SILENCE_FLOOR_DB:
            logger.debug("Сигнал — тишина (RMS=%.1f дБ), усиление не применяется.", input_rms_db)
            return GainResult(
                audio=audio.copy(),
                gain_applied_db=0.0,
                input_rms_db=input_rms_db,
                output_rms_db=input_rms_db,
                clipped_samples=0,
            )

        gain_db = target_db - input_rms_db
        gain_linear = _db_to_linear(gain_db)

        amplified = audio.astype(np.float64) * gain_linear

        # Soft-knee limiter
        limited, clipped_samples = self._soft_knee_limit(amplified)

        output = limited.astype(np.float32)
        output_rms_db = _rms_db(output)

        logger.debug(
            "GainNormalizer: вход=%.1f дБ, усиление=%.1f дБ, выход=%.1f дБ, клипп.=%d",
            input_rms_db,
            gain_db,
            output_rms_db,
            clipped_samples,
        )

        return GainResult(
            audio=output,
            gain_applied_db=round(gain_db, 3),
            input_rms_db=round(input_rms_db, 3),
            output_rms_db=round(output_rms_db, 3),
            clipped_samples=clipped_samples,
        )

    def auto_gain(self, audio: np.ndarray) -> GainResult:
        """Автоматически определяет и применяет оптимальное усиление.

        Стратегия:
        - Если пиковая амплитуда > 0.95 → нормализует к -1 дБFS по пику,
          чтобы максимально использовать динамический диапазон без клиппинга.
        - Иначе → нормализует по RMS к -20 дБFS (стандарт для распознавания речи).

        Всегда применяет soft-knee limiter.

        Args:
            audio: numpy-массив float32/float64.

        Returns:
            GainResult с автоматически подобранным усилением.
        """
        audio = self._to_mono_float32(audio)

        input_rms_db = _rms_db(audio)
        if input_rms_db <= _SILENCE_FLOOR_DB:
            return GainResult(
                audio=audio.copy(),
                gain_applied_db=0.0,
                input_rms_db=input_rms_db,
                output_rms_db=input_rms_db,
                clipped_samples=0,
            )

        peak = float(np.max(np.abs(audio)))
        if peak > _LIMITER_THRESHOLD:
            # Высокий пик — нормализуем по пику к -1 дБFS
            target_peak = _db_to_linear(-1.0)  # ≈ 0.891
            gain_linear = target_peak / peak
            gain_db = _linear_to_db(gain_linear)
        else:
            # Тихий сигнал — нормализуем по RMS к -20 дБFS
            gain_db = _DEFAULT_TARGET_DB - input_rms_db
            gain_linear = _db_to_linear(gain_db)

        amplified = audio.astype(np.float64) * gain_linear
        limited, clipped_samples = self._soft_knee_limit(amplified)
        output = limited.astype(np.float32)
        output_rms_db = _rms_db(output)

        logger.debug(
            "auto_gain: пик=%.3f, усиление=%.1f дБ, выход=%.1f дБ, клипп.=%d",
            peak,
            gain_db,
            output_rms_db,
            clipped_samples,
        )

        return GainResult(
            audio=output,
            gain_applied_db=round(gain_db, 3),
            input_rms_db=round(input_rms_db, 3),
            output_rms_db=round(output_rms_db, 3),
            clipped_samples=clipped_samples,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _to_mono_float32(audio: np.ndarray) -> np.ndarray:
        """Приводит аудио к mono float32. Многоканальное усредняет по оси 1."""
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return np.asarray(audio, dtype=np.float32)

    @staticmethod
    def _soft_knee_limit(audio: np.ndarray) -> tuple[np.ndarray, int]:
        """Мягкое ограничение пиков (soft-knee limiter).

        Алгоритм:
        - Семплы ниже threshold остаются неизменными.
        - Семплы в зоне колена [threshold, threshold + knee_linear] плавно
          компрессируются по квадратичной кривой (нет резких артефактов).
        - Семплы выше колена жёстко обрезаются до _HARD_CLIP.

        Args:
            audio: float64-массив (может выходить за [-1, 1]).

        Returns:
            (ограниченный массив float64, число обрезанных семплов).
        """
        threshold = float(_LIMITER_THRESHOLD)
        # Ширина колена в линейных единицах (от threshold до threshold + knee)
        knee_width_db = _LIMITER_KNEE_DB
        knee_top = threshold * _db_to_linear(knee_width_db)  # порог верха колена
        knee_top = min(knee_top, _HARD_CLIP)

        abs_audio = np.abs(audio)
        output = audio.copy()

        # --- Зона колена: threshold < |x| < knee_top ---
        knee_mask = (abs_audio > threshold) & (abs_audio < knee_top)
        if np.any(knee_mask):
            # Нормализуем позицию в зоне колена: 0 (у threshold) → 1 (у knee_top)
            t = (abs_audio[knee_mask] - threshold) / (knee_top - threshold)
            # Квадратичная кривая: gain снижается от 1 к threshold/knee_top
            compressed_abs = threshold + (knee_top - threshold) * (2 * t - t ** 2) * 0.5
            # Масштабируем знаком оригинала
            output[knee_mask] = np.sign(audio[knee_mask]) * compressed_abs

        # --- Жёсткое ограничение выше колена ---
        hard_mask = abs_audio >= knee_top
        clipped_samples = int(np.sum(hard_mask))
        if clipped_samples > 0:
            output[hard_mask] = np.sign(audio[hard_mask]) * _HARD_CLIP

        return output, clipped_samples
