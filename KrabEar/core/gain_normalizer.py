"""Нормализация усиления аудио перед STT.

GainNormalizer приводит входной сигнал к целевому уровню RMS (в дБFS),
применяет мягкое ограничение пиков (soft-knee limiter) для предотвращения
клиппинга и возвращает подробный GainResult с диагностикой.

Только numpy — без внешних зависимостей.

# DEAD CODE — not wired in v2.0.5; kept for resurrection.
# Wiring deferred: requires careful tuning to avoid clipping/distortion on
# the STT audio path (engine.py). Tracked as W1064 F4.
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

# BUG 1 fix: cap maximum gain to prevent near-silence signals from being
# amplified into a square wave before the limiter can act.
# Rationale: +30 dB (31.6×) is already aggressive for speech normalisation;
# beyond that the limiter hard-clips virtually the entire waveform, destroying
# STT quality. Real speech arriving at the STT path should never be below
# −50 dBFS RMS (target=-20, so max needed gain ≈ 30 dB). Signals quieter
# than that are almost certainly not speech and should be left untouched rather
# than turned into square waves.
_MAX_GAIN_DB: float = 30.0


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
    2. Вычисляет коэффициент усиления для достижения target_db,
       ограниченный сверху _MAX_GAIN_DB (+30 дБ) для защиты от
       экстремального усиления тишины в квадратную волну.
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
            Если требуемое усиление превышает _MAX_GAIN_DB (+30 дБ),
            усиление ограничивается и в лог пишется предупреждение.

        Raises:
            ValueError: если target_db > 0 (неверный знак — было бы усиление
                        выше 0 дБFS, что гарантированно приводит к клиппингу).
        """
        if target_db > 0:
            raise ValueError(
                f"target_db должен быть ≤ 0 дБFS, получено {target_db}. "
                "Положительные значения гарантируют клиппинг."
            )

        audio = self._to_mono_float32(audio)

        # Защита от NaN/Inf во входном сигнале (F1/F2)
        if len(audio) > 0 and not np.all(np.isfinite(audio)):
            logger.warning(
                "gain_normalizer: non-finite samples in input (NaN/Inf), "
                "returning audio unchanged."
            )
            return GainResult(
                audio=audio.copy(),
                gain_applied_db=0.0,
                input_rms_db=_SILENCE_FLOOR_DB,
                output_rms_db=_SILENCE_FLOOR_DB,
                clipped_samples=0,
            )

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

        # BUG 1 fix: cap extreme amplification on near-silence signals.
        # Without this cap a signal at −79 dBFS would get +59 dB (891×),
        # driving the entire waveform into hard-clip → square wave → STT garbage.
        if gain_db > _MAX_GAIN_DB:
            logger.warning(
                "gain_normalizer: required gain %.1f dB exceeds cap %.1f dB "
                "(input RMS=%.1f dBFS); capping to prevent square-wave clipping. "
                "Signal is likely near-silence or non-speech.",
                gain_db,
                _MAX_GAIN_DB,
                input_rms_db,
            )
            gain_db = _MAX_GAIN_DB

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

        # BUG 4 fix: add NaN/Inf guard (mirror of normalize() guard).
        if len(audio) > 0 and not np.all(np.isfinite(audio)):
            logger.warning(
                "gain_normalizer: auto_gain: non-finite samples in input (NaN/Inf), "
                "returning audio unchanged."
            )
            return GainResult(
                audio=audio.copy(),
                gain_applied_db=0.0,
                input_rms_db=_SILENCE_FLOOR_DB,
                output_rms_db=_SILENCE_FLOOR_DB,
                clipped_samples=0,
            )

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
        - Семплы в зоне колена [threshold, knee_top] плавно
          компрессируются по квадратичной кривой (нет резких артефактов).
          Кривая: compressed = threshold + (knee_top − threshold) × t²,
          где t = 0 у threshold, t = 1 у knee_top.
          Это даёт непрерывный переход: f(0)=threshold, f(1)=knee_top.
        - Семплы выше колена жёстко обрезаются до _HARD_CLIP.

        BUG 2 fix: knee_top теперь вычисляется как _HARD_CLIP минус ширина
        колена в линейных единицах, что делает _LIMITER_KNEE_DB реальным
        параметром (а не мёртвой ручкой).
        Старая формула: knee_top = threshold * _db_to_linear(knee_width_db)
        давала ≈ 0.95 × 1.995 ≈ 1.895 → min(1.895, 1.0) = 1.0, т.е. колено
        всегда было [0.95, 1.0] независимо от knee_width_db.
        Новая формула: knee_top = _HARD_CLIP − (_HARD_CLIP − threshold) × (1 − 10^(−knee_width_db/20))
        Для knee_width_db=6: knee_top ≈ 1.0 − 0.05 × 0.498 ≈ 0.975 → зона
        [0.95, 0.975]; при knee_width_db=0.1 зона практически нулевая.

        BUG 3 fix: кривая в зоне колена использует t² (а не (2t − t²) × 0.5),
        что обеспечивает строгое достижение knee_top при t=1 и устраняет
        разрыв у верхней границы колена.

        Args:
            audio: float64-массив (может выходить за [-1, 1]).

        Returns:
            (ограниченный массив float64, число обрезанных семплов).
        """
        threshold = float(_LIMITER_THRESHOLD)
        knee_width_db = _LIMITER_KNEE_DB

        # BUG 2 fix: compute knee_top as a dB-width zone *below* _HARD_CLIP.
        # knee_width_db=6 means the knee spans the upper 6 dB below _HARD_CLIP.
        # Formula: knee_top = HARD_CLIP - (HARD_CLIP - threshold) * (1 - 10^(-knee/20))
        # This keeps knee_top strictly below _HARD_CLIP and makes the knee_width
        # param actually change the knee zone width.
        knee_fraction = 1.0 - _db_to_linear(-knee_width_db)  # = 1 - 10^(-knee/20)
        knee_top = _HARD_CLIP - (_HARD_CLIP - threshold) * knee_fraction
        # Safety clamp: knee_top must be in (threshold, _HARD_CLIP)
        knee_top = max(threshold + 1e-9, min(knee_top, _HARD_CLIP - 1e-9))

        abs_audio = np.abs(audio)
        output = audio.copy()

        # --- Зона колена: threshold < |x| <= knee_top ---
        knee_mask = (abs_audio > threshold) & (abs_audio <= knee_top)
        if np.any(knee_mask):
            # Нормализуем позицию в зоне колена: 0 (у threshold) → 1 (у knee_top)
            t = (abs_audio[knee_mask] - threshold) / (knee_top - threshold)
            # BUG 3 fix: use t² so that at t=1 the output exactly equals knee_top.
            # Old formula: threshold + (knee_top - threshold) * (2*t - t²) * 0.5
            # evaluates to threshold + (knee_top - threshold) * 0.5 at t=1
            # (midpoint), creating a step discontinuity at the hard-clip boundary.
            # New formula: threshold + (knee_top - threshold) * t²
            # evaluates to knee_top at t=1, ensuring continuity at both ends.
            compressed_abs = threshold + (knee_top - threshold) * t ** 2
            # Масштабируем знаком оригинала
            output[knee_mask] = np.sign(audio[knee_mask]) * compressed_abs

        # --- Жёсткое ограничение выше колена ---
        hard_mask = abs_audio > knee_top
        clipped_samples = int(np.sum(hard_mask))
        if clipped_samples > 0:
            output[hard_mask] = np.sign(audio[hard_mask]) * _HARD_CLIP

        return output, clipped_samples
