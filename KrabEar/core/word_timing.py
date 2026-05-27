"""word_timing.py — анализ временных меток слов и ритма речи Krab Ear.

Вычисляет характеристики ритма речи по пословным временным меткам из
Whisper-сегментов: длительность слов, паузы, consistency темпа, колебания,
хезитации. Работает без внешних зависимостей.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, asdict
from typing import List

logger = logging.getLogger(__name__)


# ── Пороги хезитации и пауз ──────────────────────────────────────────────────

# Пауза между словами > 0.5 с в середине предложения → хезитация
_HESITATION_THRESHOLD_SEC: float = 0.5

# Пауза < 0.08 с — считается внутрисловной (артикуляционная), не межсловной
_MIN_INTER_WORD_PAUSE_SEC: float = 0.08


# ── Вспомогательные функции ──────────────────────────────────────────────────


def _extract_words(segments: List[dict]) -> List[dict]:
    """Извлекает пословные временные метки из Whisper-сегментов.

    Whisper может возвращать слова двумя способами:
    1. Поле ``words`` внутри каждого сегмента (word-level timestamps).
    2. Сам сегмент имеет ``start``/``end`` без слов — тогда весь сегмент
       рассматривается как одно «слово» (fallback).

    Возвращает список словарей вида ``{"word": str, "start": float, "end": float}``.
    """
    result: List[dict] = []
    for seg in segments:
        words = seg.get("words")
        if words:
            for w in words:
                if isinstance(w, dict) and "start" in w and "end" in w:
                    start = float(w["start"])
                    end = float(w["end"])
                    if not (math.isfinite(start) and math.isfinite(end) and end > start):
                        continue  # skip non-finite or invalid pairs
                    result.append({"word": str(w.get("word", "")), "start": start, "end": end})
        else:
            # Fallback: сегмент без пословных меток
            logger.debug("word_timing: words field absent, falling back to segment-level (coarse)")
            start = seg.get("start")
            end = seg.get("end")
            if start is not None and end is not None:
                start = float(start)
                end = float(end)
                if end > start:
                    text = seg.get("text", seg.get("word", ""))
                    result.append({"word": str(text), "start": start, "end": end})
    return result


@dataclass
class TimingReport:
    """Результат анализа временных меток слов."""

    avg_word_duration_ms: float
    """Средняя длительность одного слова, миллисекунды."""

    avg_pause_duration_ms: float
    """Средняя длительность паузы между словами, миллисекунды."""

    total_pause_time_sec: float
    """Суммарное время пауз (inter-word), секунды."""

    speaking_rate_consistency: float
    """Равномерность темпа речи: 0.0 — хаотично, 1.0 — очень равномерно.

    Вычисляется как ``1 / (1 + CV)`` где CV — коэффициент вариации
    длительностей слов. При отсутствии данных — 0.0.
    """

    longest_pause_sec: float
    """Длиннейшая пауза между словами, секунды."""

    hesitation_count: int
    """Количество хезитаций: пауз > 0.5 с в середине фразы."""

    def as_dict(self) -> dict:
        """Сериализует отчёт в словарь для IPC-ответа."""
        return asdict(self)


class WordTimingAnalyzer:
    """Анализирует ритм речи по пословным временным меткам Whisper.

    Принимает список сегментов формата Whisper (каждый сегмент может содержать
    поле ``words`` с пословными метками start/end). При их отсутствии
    работает на уровне сегментов.

    Пример::

        analyzer = WordTimingAnalyzer()
        segments = [
            {
                "start": 0.0, "end": 2.0,
                "words": [
                    {"word": "Hello", "start": 0.0, "end": 0.4},
                    {"word": "world", "start": 0.5, "end": 0.9},
                ]
            }
        ]
        report = analyzer.analyze(segments)
        logger.info(f"Timing: {report.avg_word_duration_ms}ms, hesitations: {report.hesitation_count}")
    """

    def analyze(self, segments: List[dict]) -> TimingReport:
        """Анализирует временные метки и возвращает TimingReport.

        Args:
            segments: список Whisper-сегментов. Каждый должен содержать
                ``start``, ``end`` и опционально ``words``.

        Returns:
            TimingReport с вычисленными метриками ритма.
        """
        words = _extract_words(segments)

        if not words:
            return self._empty_report()

        # ── Длительности слов ────────────────────────────────────────────
        durations_ms = [(w["end"] - w["start"]) * 1000.0 for w in words]
        avg_word_duration_ms = sum(durations_ms) / len(durations_ms)

        # ── Паузы между словами ─────────────────────────────────────────
        # Сортируем слова по началу для корректного определения пауз
        sorted_words = sorted(words, key=lambda w: w["start"])
        pauses_sec: List[float] = []
        for i in range(1, len(sorted_words)):
            gap = sorted_words[i]["start"] - sorted_words[i - 1]["end"]
            if gap >= _MIN_INTER_WORD_PAUSE_SEC:
                pauses_sec.append(gap)

        total_pause_time_sec = sum(pauses_sec)
        longest_pause_sec = max(pauses_sec) if pauses_sec else 0.0
        avg_pause_duration_ms = (
            (sum(pauses_sec) / len(pauses_sec)) * 1000.0 if pauses_sec else 0.0
        )

        # ── Хезитации (паузы > 0.5 с не в конце фразы) ─────────────────
        # Последняя пауза может быть между предложениями — не считаем
        mid_pauses = pauses_sec[:-1] if len(pauses_sec) > 1 else pauses_sec
        hesitation_count = sum(1 for p in mid_pauses if p > _HESITATION_THRESHOLD_SEC)

        # ── Равномерность темпа (1 / (1 + CV)) ─────────────────────────
        speaking_rate_consistency = self._compute_consistency(durations_ms)

        return TimingReport(
            avg_word_duration_ms=round(avg_word_duration_ms, 2),
            avg_pause_duration_ms=round(avg_pause_duration_ms, 2),
            total_pause_time_sec=round(total_pause_time_sec, 3),
            speaking_rate_consistency=round(speaking_rate_consistency, 4),
            longest_pause_sec=round(longest_pause_sec, 3),
            hesitation_count=hesitation_count,
        )

    # ── Вспомогательные методы ───────────────────────────────────────────────

    @staticmethod
    def _compute_consistency(durations_ms: List[float]) -> float:
        """Вычисляет коэффициент равномерности темпа речи (0–1).

        Использует коэффициент вариации (std/mean). При нулевом среднем или
        единственном слове возвращает 1.0 (максимально равномерно по умолчанию).
        """
        if len(durations_ms) < 2:
            return 1.0
        mean = sum(durations_ms) / len(durations_ms)
        if mean <= 0:
            return 0.0
        std = statistics.stdev(durations_ms)
        cv = std / mean
        return 1.0 / (1.0 + cv)

    @staticmethod
    def _empty_report() -> TimingReport:
        """Возвращает нулевой TimingReport для пустого/невалидного ввода."""
        return TimingReport(
            avg_word_duration_ms=0.0,
            avg_pause_duration_ms=0.0,
            total_pause_time_sec=0.0,
            speaking_rate_consistency=0.0,
            longest_pause_sec=0.0,
            hesitation_count=0,
        )
