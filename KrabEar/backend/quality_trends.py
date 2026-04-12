"""Анализ трендов качества записей Krab Ear.

QualityTrendAnalyzer — вычисляет дневные агрегаты confidence, линейный тренд,
лучший/худший день и гистограмму распределения оценок качества.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class TrendReport:
    """Результат анализа тренда качества за период."""

    daily_confidence: list[dict]
    """[{"date": "YYYY-MM-DD", "avg": 0.87, "min": 0.5, "max": 0.99, "count": 15}]"""

    overall_trend: str
    """'improving' | 'stable' | 'declining'"""

    trend_slope: float
    """Наклон линейной регрессии по дням (положительный = улучшение)."""

    best_day: dict
    """Строка daily_confidence с наивысшим avg, либо {} если нет данных."""

    worst_day: dict
    """Строка daily_confidence с наименьшим avg, либо {} если нет данных."""

    confidence_distribution: dict
    """{"0.9-1.0": 50, "0.8-0.9": 30, "0.7-0.8": 15, "0.6-0.7": 5, "0.0-0.6": 2}"""


class QualityTrendAnalyzer:
    """Вычисляет тренды качества транскрипций по записям истории."""

    # Порог наклона (за день) для классификации тренда.
    _SLOPE_IMPROVING = 0.001
    _SLOPE_DECLINING = -0.001

    # Бакеты гистограммы в порядке убывания качества.
    _BUCKETS = [
        ("0.9-1.0", 0.9, 1.0),
        ("0.8-0.9", 0.8, 0.9),
        ("0.7-0.8", 0.7, 0.8),
        ("0.6-0.7", 0.6, 0.7),
        ("0.0-0.6", 0.0, 0.6),
    ]

    def analyze_trends(self, items: list[Any], days: int = 30) -> TrendReport:
        """Анализирует тренды качества по списку элементов истории.

        Args:
            items: список объектов/словарей истории с полями ``ts`` и ``confidence``.
                   Поле ``ts`` может быть ISO-строкой или epoch float.
                   Поле ``confidence`` — float 0.0–1.0 или None.
            days: количество дней окна анализа.

        Returns:
            TrendReport с агрегатами, трендом и гистограммой.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Группируем confidence по датам.
        daily: dict[str, list[float]] = {}
        all_confidences: list[float] = []

        for item in items:
            confidence = self._get_confidence(item)
            if confidence is None:
                continue

            ts = self._get_ts(item)
            if ts is None or ts < cutoff:
                continue

            date_str = ts.date().isoformat()
            daily.setdefault(date_str, []).append(confidence)
            all_confidences.append(confidence)

        # Строим отсортированный список дневных агрегатов.
        daily_confidence = []
        for date_str in sorted(daily.keys()):
            vals = daily[date_str]
            daily_confidence.append({
                "date": date_str,
                "avg": round(sum(vals) / len(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "count": len(vals),
            })

        if not daily_confidence:
            return TrendReport(
                daily_confidence=[],
                overall_trend="stable",
                trend_slope=0.0,
                best_day={},
                worst_day={},
                confidence_distribution=self._build_distribution([]),
            )

        # Линейная регрессия по (x=индекс_дня, y=avg_confidence).
        slope = self._linear_regression_slope(
            [d["avg"] for d in daily_confidence]
        )

        # Классификация тренда.
        if slope > self._SLOPE_IMPROVING:
            overall_trend = "improving"
        elif slope < self._SLOPE_DECLINING:
            overall_trend = "declining"
        else:
            overall_trend = "stable"

        best_day = max(daily_confidence, key=lambda d: d["avg"])
        worst_day = min(daily_confidence, key=lambda d: d["avg"])

        return TrendReport(
            daily_confidence=daily_confidence,
            overall_trend=overall_trend,
            trend_slope=round(slope, 6),
            best_day=best_day,
            worst_day=worst_day,
            confidence_distribution=self._build_distribution(all_confidences),
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _get_confidence(item: Any) -> float | None:
        """Извлекает confidence из объекта или словаря."""
        if isinstance(item, dict):
            val = item.get("confidence")
        else:
            val = getattr(item, "confidence", None)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_ts(item: Any) -> datetime | None:
        """Извлекает timestamp как timezone-aware datetime."""
        if isinstance(item, dict):
            raw = item.get("ts")
        else:
            raw = getattr(item, "ts", None)

        if raw is None:
            return None

        # Уже datetime
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc)
            return raw

        # epoch float/int
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)

        # ISO-строка
        if isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                return None

        return None

    @staticmethod
    def _linear_regression_slope(values: list[float]) -> float:
        """Вычисляет наклон методом наименьших квадратов без numpy."""
        n = len(values)
        if n < 2:
            return 0.0

        x_mean = (n - 1) / 2.0
        y_mean = sum(values) / n

        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 0.0

        return numerator / denominator

    def _build_distribution(self, confidences: list[float]) -> dict:
        """Строит гистограмму по предопределённым бакетам."""
        dist: dict[str, int] = {label: 0 for label, _, _ in self._BUCKETS}
        for c in confidences:
            for label, lo, hi in self._BUCKETS:
                if lo <= c <= hi:
                    dist[label] += 1
                    break
        return dist

    def to_dict(self, report: TrendReport) -> dict:
        """Сериализует TrendReport в plain dict для JSON-RPC ответа."""
        return {
            "daily_confidence": report.daily_confidence,
            "overall_trend": report.overall_trend,
            "trend_slope": report.trend_slope,
            "best_day": report.best_day,
            "worst_day": report.worst_day,
            "confidence_distribution": report.confidence_distribution,
        }
