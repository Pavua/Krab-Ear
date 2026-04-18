"""Умный выбор STT-модели на основе условий записи.

Выбирает оптимальную модель Whisper автоматически, учитывая:
- режим превью (is_preview)
- длительность аудио
- требуемый профиль качества
- системную нагрузку

Пример использования::

    selector = SmartModelSelector()
    sel = selector.select_model(duration_sec=45.0, quality="max", is_preview=False)
    logger.info(f"Selected: {sel.model_name}, reason: {sel.reason}")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

# Latency coefficients: RTF (real-time factor) — примерная скорость обработки
# на Apple Silicon M-серии. balanced-модель быстрее, max — точнее.
_RTF_BALANCED = 0.15   # ~15% от длительности аудио
_RTF_MAX = 0.35        # ~35% от длительности аудио
_LATENCY_OVERHEAD_MS = 200  # фиксированный оверхед инициализации (мс)

# Пороги для логики выбора
_PREVIEW_MAX_SEC = 3.0   # превью: записи короче 3 с
_SHORT_MAX_SEC = 10.0    # короткая запись: < 10 с
_LONG_MIN_SEC = 60.0     # длинная запись: > 60 с
_HIGH_LOAD_THRESHOLD = 0.75  # высокая нагрузка CPU (0–1)

# TTL кэша списка моделей (секунды)
_MODELS_CACHE_TTL = 60.0


@dataclass
class ModelSelection:
    """Результат выбора модели."""
    model_name: str
    reason: str
    estimated_latency_ms: float
    quality_tier: str  # "balanced" | "max"


class SmartModelSelector:
    """Автоматически выбирает STT-модель на основе текущих условий."""

    def __init__(self) -> None:
        # Кэш списка моделей: (timestamp, data)
        self._models_cache: tuple[float, list[dict[str, Any]]] | None = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def select_model(
        self,
        duration_sec: float,
        quality: str,
        is_preview: bool,
        system_load: float = 0.0,
    ) -> ModelSelection:
        """Выбирает оптимальную STT-модель.

        Логика приоритетов (по убыванию):

        1. Превью-режим (is_preview=True или duration_sec < 3с) →
           всегда balanced (скорость важнее точности).
        2. Профиль «balanced» → всегда balanced.
        3. Профиль «max» + короткая запись (<10с) → max (успеет быстро).
        4. Длинная запись (>60с) + высокая нагрузка → balanced (экономия).
        5. Всё остальное при quality=max → max.
        6. Fallback → balanced.

        Аргументы:
            duration_sec: длительность аудио в секундах.
            quality: "balanced" | "max" (регистронезависимо).
            is_preview: True если это превью-транскрибация.
            system_load: нагрузка CPU 0.0–1.0 (по умолчанию 0 = не учитывается).

        Возвращает:
            ModelSelection с выбранной моделью и обоснованием.
        """
        quality_norm = str(quality).strip().lower()
        balanced_name = settings.MODEL_BALANCED
        max_name = settings.model_max_list[0]  # первый кандидат max-профиля

        # --- Правило 1: превью или очень короткая запись ---
        if is_preview or duration_sec < _PREVIEW_MAX_SEC:
            return ModelSelection(
                model_name=balanced_name,
                reason="preview mode — fastest model selected",
                estimated_latency_ms=self.estimate_latency(balanced_name, duration_sec),
                quality_tier="balanced",
            )

        # --- Правило 2: явный запрос balanced ---
        if quality_norm == "balanced":
            return ModelSelection(
                model_name=balanced_name,
                reason="quality=balanced — fast model selected",
                estimated_latency_ms=self.estimate_latency(balanced_name, duration_sec),
                quality_tier="balanced",
            )

        # --- Правило 3: max + короткая запись ---
        if quality_norm == "max" and duration_sec < _SHORT_MAX_SEC:
            return ModelSelection(
                model_name=max_name,
                reason="quality=max, short recording — max model fits within latency budget",
                estimated_latency_ms=self.estimate_latency(max_name, duration_sec),
                quality_tier="max",
            )

        # --- Правило 4: длинная запись + высокая нагрузка ---
        if duration_sec > _LONG_MIN_SEC and system_load >= _HIGH_LOAD_THRESHOLD:
            return ModelSelection(
                model_name=balanced_name,
                reason=(
                    f"long recording ({duration_sec:.0f}s) + high system load "
                    f"({system_load:.0%}) — balanced model to save resources"
                ),
                estimated_latency_ms=self.estimate_latency(balanced_name, duration_sec),
                quality_tier="balanced",
            )

        # --- Правило 5: max без ограничений ---
        if quality_norm == "max":
            return ModelSelection(
                model_name=max_name,
                reason="quality=max — max accuracy model selected",
                estimated_latency_ms=self.estimate_latency(max_name, duration_sec),
                quality_tier="max",
            )

        # --- Fallback ---
        return ModelSelection(
            model_name=balanced_name,
            reason="default fallback — balanced model selected",
            estimated_latency_ms=self.estimate_latency(balanced_name, duration_sec),
            quality_tier="balanced",
        )

    def get_available_models(self) -> list[dict[str, Any]]:
        """Возвращает список доступных STT-моделей с метаданными.

        Результат кэшируется на _MODELS_CACHE_TTL секунд.
        """
        now = time.monotonic()
        if self._models_cache is not None:
            cached_ts, cached_data = self._models_cache
            if now - cached_ts < _MODELS_CACHE_TTL:
                return cached_data

        balanced = settings.MODEL_BALANCED
        max_candidates = settings.model_max_list

        models: list[dict[str, Any]] = [
            {
                "name": balanced,
                "tier": "balanced",
                "description": "Fast, low-latency model for real-time and preview use",
                "rtf": _RTF_BALANCED,
                "is_default": True,
            }
        ]

        seen = {balanced}
        for candidate in max_candidates:
            if candidate not in seen:
                models.append(
                    {
                        "name": candidate,
                        "tier": "max",
                        "description": "High-accuracy model for quality-critical transcription",
                        "rtf": _RTF_MAX,
                        "is_default": False,
                    }
                )
                seen.add(candidate)

        self._models_cache = (now, models)
        return models

    def estimate_latency(self, model: str, duration_sec: float) -> float:
        """Оценивает ожидаемую задержку обработки в миллисекундах.

        Формула: RTF × duration_sec × 1000 + overhead_ms

        Аргументы:
            model: имя модели (используется для выбора RTF-коэффициента).
            duration_sec: длительность аудио в секундах.

        Возвращает:
            Ожидаемая задержка в миллисекундах (float).
        """
        balanced = settings.MODEL_BALANCED
        max_candidates = settings.model_max_list

        if model == balanced:
            rtf = _RTF_BALANCED
        elif model in max_candidates:
            rtf = _RTF_MAX
        else:
            # Неизвестная модель — предполагаем max RTF как пессимистичную оценку
            rtf = _RTF_MAX

        return max(0.0, duration_sec) * rtf * 1000.0 + _LATENCY_OVERHEAD_MS
