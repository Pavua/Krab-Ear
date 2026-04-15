"""Оценщик стоимости вычислений для записей Krab Ear.

Оценивает приблизительные затраты вычислительных ресурсов:
- compute_time_sec   — ожидаемое время обработки
- memory_mb          — пиковая память
- disk_mb            — место на диске для результата
- features_cost      — разбивка по компонентам (STT, diarization, LLM, translation)
- total_relative_cost — нормализованный 0-1 (1 = максимальная конфигурация)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger("KrabEar.Backend.CostEstimator")

# ---------------------------------------------------------------------------
# Базовые коэффициенты (секунды обработки на секунду аудио)
# ---------------------------------------------------------------------------
_STT_RATES: dict[str, float] = {
    "balanced": 0.3,
    "max": 0.5,
    "remote": 0.1,  # remote STT быстрее по локальным ресурсам
}

# Дополнительные коэффициенты поверх STT
_DIARIZATION_MULTIPLIER = 2.0   # +100% к времени STT
_LLM_FLAT_SEC = 0.5             # плоская добавка на LLM rewrite (секунды)
_TRANSLATION_FLAT_SEC = 0.2     # плоская добавка на перевод

# Память (МБ) по quality-профилю
_MEMORY_MB: dict[str, float] = {
    "balanced": 900.0,
    "max": 1800.0,
    "remote": 200.0,
}
_DIARIZATION_MEM_MB = 400.0
_LLM_MEM_MB = 300.0
_TRANSLATION_MEM_MB = 150.0

# Диск: оценка хранилища результата (МБ на минуту аудио)
_DISK_MB_PER_MIN = 0.05   # ~3 КБ/мин для текстовых транскрипций + метаданные

# Нормирование: "максимальная конфигурация" = max quality + diarization + LLM + translation, 60 мин
_MAX_REFERENCE_SEC = 60 * 60.0  # 60 минут аудио
_MAX_COMPUTE_SEC = (
    _STT_RATES["max"] * _MAX_REFERENCE_SEC * _DIARIZATION_MULTIPLIER
    + _LLM_FLAT_SEC
    + _TRANSLATION_FLAT_SEC
)


@dataclass
class CostEstimate:
    """Оценка стоимости обработки одной записи."""

    compute_time_sec: float
    """Ожидаемое время обработки (секунды)."""

    memory_mb: float
    """Пиковое потребление памяти (МБ)."""

    disk_mb: float
    """Объём хранилища для результата (МБ)."""

    features_cost: dict[str, float] = field(default_factory=dict)
    """Разбивка вычислительного времени по компонентам:
    stt, diarization, llm, translation."""

    total_relative_cost: float = 0.0
    """Нормализованная относительная стоимость [0..1].
    1.0 соответствует 60 минутам при максимальной конфигурации."""


class CostEstimator:
    """Оценивает вычислительную стоимость транскрибации записей Krab Ear."""

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def estimate_cost(
        self,
        duration_sec: float,
        quality: str = "balanced",
        features: dict[str, Any] | None = None,
    ) -> CostEstimate:
        """Оценивает стоимость обработки одной записи.

        Параметры:
            duration_sec  — длительность аудио в секундах (>= 0).
            quality       — профиль STT: "balanced", "max", "remote".
            features      — включённые компоненты:
                            {
                              "diarization": bool,
                              "llm": bool,
                              "translation": bool,
                            }
        Возвращает:
            CostEstimate с полями compute_time_sec, memory_mb, disk_mb,
            features_cost, total_relative_cost.
        """
        duration_sec = max(0.0, float(duration_sec))
        quality = quality if quality in _STT_RATES else "balanced"
        features = features or {}

        diarization = bool(features.get("diarization", False))
        llm = bool(features.get("llm", False))
        translation = bool(features.get("translation", False))

        # --- STT ---
        stt_rate = _STT_RATES[quality]
        stt_compute = stt_rate * duration_sec

        # --- Diarization ---
        diarization_compute = 0.0
        if diarization:
            diarization_compute = stt_compute * (_DIARIZATION_MULTIPLIER - 1.0)

        # --- LLM rewrite ---
        llm_compute = _LLM_FLAT_SEC if llm else 0.0

        # --- Translation ---
        translation_compute = _TRANSLATION_FLAT_SEC if translation else 0.0

        total_compute = stt_compute + diarization_compute + llm_compute + translation_compute

        # --- Memory ---
        base_mem = _MEMORY_MB.get(quality, _MEMORY_MB["balanced"])
        total_mem = base_mem
        if diarization:
            total_mem += _DIARIZATION_MEM_MB
        if llm:
            total_mem += _LLM_MEM_MB
        if translation:
            total_mem += _TRANSLATION_MEM_MB

        # --- Disk ---
        disk = _DISK_MB_PER_MIN * (duration_sec / 60.0)

        # --- Relative cost ---
        relative = min(1.0, total_compute / _MAX_COMPUTE_SEC) if _MAX_COMPUTE_SEC > 0 else 0.0

        return CostEstimate(
            compute_time_sec=round(total_compute, 4),
            memory_mb=round(total_mem, 2),
            disk_mb=round(disk, 6),
            features_cost={
                "stt": round(stt_compute, 4),
                "diarization": round(diarization_compute, 4),
                "llm": round(llm_compute, 4),
                "translation": round(translation_compute, 4),
            },
            total_relative_cost=round(relative, 6),
        )

    def estimate_batch_cost(self, files: list[dict[str, Any]]) -> dict[str, Any]:
        """Оценивает суммарную стоимость пакетного импорта.

        Каждый элемент files: {"duration_sec": float, "quality": str, "features": dict}.
        Отсутствующие поля заменяются значениями по умолчанию.

        Возвращает:
            {
              "total_compute_time_sec": float,
              "total_memory_mb": float,       # пик (максимум по файлам)
              "total_disk_mb": float,
              "file_count": int,
              "estimates": [CostEstimate-like dict, ...],
            }
        """
        estimates: list[CostEstimate] = []
        for item in files:
            est = self.estimate_cost(
                duration_sec=float(item.get("duration_sec", 0.0)),
                quality=str(item.get("quality", "balanced")),
                features=item.get("features") or {},
            )
            estimates.append(est)

        total_compute = sum(e.compute_time_sec for e in estimates)
        peak_memory = max((e.memory_mb for e in estimates), default=0.0)
        total_disk = sum(e.disk_mb for e in estimates)

        return {
            "total_compute_time_sec": round(total_compute, 4),
            "total_memory_mb": round(peak_memory, 2),
            "total_disk_mb": round(total_disk, 6),
            "file_count": len(estimates),
            "estimates": [
                {
                    "compute_time_sec": e.compute_time_sec,
                    "memory_mb": e.memory_mb,
                    "disk_mb": e.disk_mb,
                    "features_cost": e.features_cost,
                    "total_relative_cost": e.total_relative_cost,
                }
                for e in estimates
            ],
        }

    def get_daily_cost_summary(self, usage_tracker: Any) -> dict[str, Any]:
        """Возвращает сводку вычислительных расходов за сегодня на основе UsageTracker.

        Параметры:
            usage_tracker — экземпляр UsageTracker (или совместимый объект с
                            get_usage_stats() -> dict).

        Возвращает:
            {
              "date": str,                    # ISO-дата сегодня
              "recordings_today": int,
              "total_duration_sec": float,
              "estimated_compute_sec": float, # balanced STT, без доп. функций
              "estimated_memory_mb": float,
              "estimated_disk_mb": float,
              "relative_cost": float,         # нормализованный 0-1
            }
        """
        try:
            stats = usage_tracker.get_usage_stats()
            today_stats = stats.get("today", {})
            total_duration = float(today_stats.get("total_duration_sec", 0.0))
            recordings = int(today_stats.get("recordings", 0))
        except Exception:
            logger.exception("Ошибка чтения usage_tracker")
            total_duration = 0.0
            recordings = 0

        # Оцениваем суммарно с balanced-профилем и без дополнительных функций
        est = self.estimate_cost(duration_sec=total_duration, quality="balanced")

        return {
            "date": date.today().isoformat(),
            "recordings_today": recordings,
            "total_duration_sec": round(total_duration, 2),
            "estimated_compute_sec": est.compute_time_sec,
            "estimated_memory_mb": est.memory_mb,
            "estimated_disk_mb": est.disk_mb,
            "relative_cost": est.total_relative_cost,
        }
