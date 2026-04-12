"""Анализ трендов тональности транскрипций Krab Ear по времени.

SentimentTrendAnalyzer — вычисляет дневные агрегаты тональности, общий балл (-1..1),
тренд настроения, распределение по категориям и лучший/худший день.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from core.emotion_detector import EmotionDetector


# Маппинг эмоций EmotionDetector на числовой балл тональности.
_EMOTION_SCORE: dict[str, float] = {
    "positive": 0.7,
    "excited": 0.9,
    "neutral": 0.0,
    "questioning": -0.1,
    "negative": -0.7,
    "frustrated": -0.9,
}


@dataclass
class SentimentTrendReport:
    """Результат анализа тренда тональности за период."""

    daily_sentiment: list[dict]
    """[{"date": "YYYY-MM-DD", "avg_sentiment": 0.42, "dominant_emotion": "positive", "count": 12}]"""

    overall_sentiment: float
    """Средний балл тональности за весь период (-1.0 до 1.0)."""

    sentiment_distribution: dict
    """{"positive": N, "negative": N, "neutral": N}"""

    mood_trend: str
    """'improving' | 'stable' | 'declining'"""

    most_positive_day: dict
    """Запись daily_sentiment с наивысшим avg_sentiment, либо {} если нет данных."""

    most_negative_day: dict
    """Запись daily_sentiment с наименьшим avg_sentiment, либо {} если нет данных."""


class SentimentTrendAnalyzer:
    """Вычисляет тренды тональности транскрипций по записям истории.

    Использует EmotionDetector для оценки тональности каждого элемента.
    """

    # Порог наклона (за день) для классификации тренда настроения.
    _SLOPE_IMPROVING = 0.005
    _SLOPE_DECLINING = -0.005

    # Языки для анализа — приоритет ru, потом en.
    _DEFAULT_LANGUAGE = "ru"

    def __init__(self, detector: EmotionDetector | None = None) -> None:
        self._detector = detector or EmotionDetector()

    def analyze_sentiment_trends(
        self, items: list[Any], days: int = 30
    ) -> SentimentTrendReport:
        """Анализирует тренды тональности по списку элементов истории.

        Args:
            items: список объектов/словарей истории с полями ``ts``, ``text``
                   и опционально ``language``.
                   Поле ``ts`` может быть ISO-строкой или epoch float.
                   Поле ``text`` — транскрибированный текст.
            days: количество дней окна анализа.

        Returns:
            SentimentTrendReport с дневными агрегатами, трендом и распределением.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Группируем баллы тональности по датам.
        daily: dict[str, list[float]] = {}
        daily_emotions: dict[str, list[str]] = {}
        all_scores: list[float] = []
        distribution: dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}

        for item in items:
            ts = self._get_ts(item)
            if ts is None or ts < cutoff:
                continue

            text = self._get_text(item)
            if not text:
                continue

            language = self._get_language(item)
            result = self._detector.detect(text, language=language)
            score = _EMOTION_SCORE.get(result.primary_emotion, 0.0)

            date_str = ts.date().isoformat()
            daily.setdefault(date_str, []).append(score)
            daily_emotions.setdefault(date_str, []).append(result.primary_emotion)
            all_scores.append(score)

            # Категоризация для distribution.
            if score > 0.1:
                distribution["positive"] += 1
            elif score < -0.1:
                distribution["negative"] += 1
            else:
                distribution["neutral"] += 1

        # Строим отсортированный список дневных агрегатов.
        daily_sentiment = []
        for date_str in sorted(daily.keys()):
            scores = daily[date_str]
            emotions = daily_emotions[date_str]
            avg = sum(scores) / len(scores)
            dominant = self._dominant_emotion(emotions)
            daily_sentiment.append({
                "date": date_str,
                "avg_sentiment": round(avg, 4),
                "dominant_emotion": dominant,
                "count": len(scores),
            })

        if not daily_sentiment:
            return SentimentTrendReport(
                daily_sentiment=[],
                overall_sentiment=0.0,
                sentiment_distribution=distribution,
                mood_trend="stable",
                most_positive_day={},
                most_negative_day={},
            )

        overall = sum(all_scores) / len(all_scores) if all_scores else 0.0

        # Линейная регрессия по (x=индекс_дня, y=avg_sentiment).
        slope = self._linear_regression_slope(
            [d["avg_sentiment"] for d in daily_sentiment]
        )

        if slope > self._SLOPE_IMPROVING:
            mood_trend = "improving"
        elif slope < self._SLOPE_DECLINING:
            mood_trend = "declining"
        else:
            mood_trend = "stable"

        most_positive = max(daily_sentiment, key=lambda d: d["avg_sentiment"])
        most_negative = min(daily_sentiment, key=lambda d: d["avg_sentiment"])

        return SentimentTrendReport(
            daily_sentiment=daily_sentiment,
            overall_sentiment=round(overall, 4),
            sentiment_distribution=distribution,
            mood_trend=mood_trend,
            most_positive_day=most_positive,
            most_negative_day=most_negative,
        )

    def to_dict(self, report: SentimentTrendReport) -> dict:
        """Сериализует SentimentTrendReport в plain dict для JSON-RPC ответа."""
        return {
            "daily_sentiment": report.daily_sentiment,
            "overall_sentiment": report.overall_sentiment,
            "sentiment_distribution": report.sentiment_distribution,
            "mood_trend": report.mood_trend,
            "most_positive_day": report.most_positive_day,
            "most_negative_day": report.most_negative_day,
        }

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _get_text(item: Any) -> str:
        """Извлекает текст транскрипции из объекта или словаря."""
        if isinstance(item, dict):
            return item.get("text") or ""
        return getattr(item, "text", "") or ""

    @staticmethod
    def _get_language(item: Any) -> str:
        """Извлекает язык из элемента; по умолчанию 'ru'."""
        if isinstance(item, dict):
            lang = item.get("language") or item.get("lang") or "ru"
        else:
            lang = getattr(item, "language", None) or getattr(item, "lang", None) or "ru"
        return str(lang).lower().split("-")[0]

    @staticmethod
    def _get_ts(item: Any) -> datetime | None:
        """Извлекает timestamp как timezone-aware datetime."""
        if isinstance(item, dict):
            raw = item.get("ts")
        else:
            raw = getattr(item, "ts", None)

        if raw is None:
            return None

        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc)
            return raw

        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)

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
    def _dominant_emotion(emotions: list[str]) -> str:
        """Возвращает наиболее часто встречающуюся эмоцию."""
        if not emotions:
            return "neutral"
        counts: dict[str, int] = {}
        for e in emotions:
            counts[e] = counts.get(e, 0) + 1
        return max(counts, key=lambda k: counts[k])

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
