"""
confidence_calibrator.py — Калибровка сырых оценок уверенности STT.

Корректирует raw confidence из mlx-whisper с учётом длительности записи,
языка и используемой модели.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import List


# Языки, для которых whisper обучен лучше всего (первичные для Krab Ear)
PRIMARY_LANGUAGES = {"ru", "es", "russian", "spanish"}

# Пороговые значения длительности записи (секунды)
_SHORT_DURATION_THRESHOLD = 2.0   # < 2s — завышенная уверенность
_LONG_DURATION_THRESHOLD = 60.0   # > 60s — ухудшение качества (накопление галлюцинаций)

# Коэффициенты коррекции
_SHORT_PENALTY = -0.10    # -10% для коротких записей
_LONG_PENALTY = -0.05     # -5% для длинных записей (деградация из-за галлюцинаций)
_NON_PRIMARY_PENALTY = -0.05  # -5% для нецелевых языков
_BALANCED_PENALTY = -0.03     # -3% для balanced-модели


@dataclass
class CalibratedScore:
    """Результат калибровки одной оценки уверенности."""
    raw: float
    calibrated: float
    adjustments: List[str] = field(default_factory=list)


class ConfidenceCalibrator:
    """
    Калибратор оценок уверенности STT.

    Применяет эмпирические поправки к raw confidence на основе:
    - длительности аудиозаписи
    - языка распознавания
    - используемой модели (balanced / max)

    Все методы потокобезопасны.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_calibrations: int = 0
        self._adjustment_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def calibrate(
        self,
        raw_confidence: float,
        duration_sec: float,
        language: str,
        model: str,
    ) -> float:
        """
        Возвращает скалиброванное значение уверенности в диапазоне [0.0, 1.0].

        :param raw_confidence: сырая оценка из mlx-whisper (0.0–1.0)
        :param duration_sec:   длительность аудио в секундах
        :param language:       код/название языка (например "ru", "russian", "en")
        :param model:          имя используемой модели
        :return:               скалиброванное значение (clamped to [0.0, 1.0])
        """
        result = self.calibrate_detailed(raw_confidence, duration_sec, language, model)
        return result.calibrated

    def calibrate_detailed(
        self,
        raw_confidence: float,
        duration_sec: float,
        language: str,
        model: str,
    ) -> CalibratedScore:
        """
        Возвращает полный объект :class:`CalibratedScore` со всеми поправками.
        """
        # F1: NaN/Inf/None вход → 0.0 (неизвестная уверенность, не 1.0)
        if raw_confidence is None or not (
            isinstance(raw_confidence, (int, float)) and math.isfinite(raw_confidence)
        ):
            return CalibratedScore(
                raw=raw_confidence,  # type: ignore[arg-type]
                calibrated=0.0,
                adjustments=["invalid_raw(nan_or_inf): forced_to_0.0"],
            )

        adjustments: list[str] = []
        delta = 0.0

        # --- Поправка по длительности ---
        if duration_sec < _SHORT_DURATION_THRESHOLD:
            delta += _SHORT_PENALTY
            adjustments.append(
                f"short_recording({duration_sec:.1f}s): {_SHORT_PENALTY:+.0%}"
            )
        elif duration_sec > _LONG_DURATION_THRESHOLD:
            # F2: длинные записи ухудшают качество (накопление галлюцинаций) — штраф
            delta += _LONG_PENALTY
            adjustments.append(
                f"long_recording({duration_sec:.1f}s): {_LONG_PENALTY:+.0%}"
            )

        # --- Поправка по языку ---
        lang_norm = (language or "").lower().strip()
        if lang_norm and lang_norm not in PRIMARY_LANGUAGES:
            delta += _NON_PRIMARY_PENALTY
            adjustments.append(
                f"non_primary_language({language}): {_NON_PRIMARY_PENALTY:+.0%}"
            )

        # --- Поправка по модели ---
        model_lower = (model or "").lower()
        if "balanced" in model_lower:
            delta += _BALANCED_PENALTY
            adjustments.append(
                f"balanced_model: {_BALANCED_PENALTY:+.0%}"
            )
        # max-модель — без поправки

        calibrated = max(0.0, min(1.0, raw_confidence + delta))
        score = CalibratedScore(
            raw=raw_confidence,
            calibrated=round(calibrated, 4),
            adjustments=adjustments,
        )

        with self._lock:
            self._total_calibrations += 1
            for adj in adjustments:
                # Используем только первое слово как ключ статистики
                key = adj.split("(")[0].split(":")[0]
                self._adjustment_counts[key] = self._adjustment_counts.get(key, 0) + 1

        return score

    def get_calibration_stats(self) -> dict:
        """
        Возвращает словарь со статистикой применённых поправок.

        Ключи:
        - ``total_calibrations`` — общее количество вызовов calibrate/calibrate_detailed
        - ``adjustment_counts``  — сколько раз каждый тип поправки был применён
        """
        with self._lock:
            return {
                "total_calibrations": self._total_calibrations,
                "adjustment_counts": dict(self._adjustment_counts),
            }

    def reset_stats(self) -> None:
        """Сбрасывает накопленную статистику (удобно в тестах)."""
        with self._lock:
            self._total_calibrations = 0
            self._adjustment_counts.clear()
