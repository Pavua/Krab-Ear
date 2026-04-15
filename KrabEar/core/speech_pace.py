"""speech_pace.py — анализ темпа речи транскрибаций Krab Ear.

Вычисляет слова в минуту (WPM), символы в минуту, категорию темпа и
расчётное время чтения. Работает без внешних зависимостей.
Адаптировано для русского, испанского и английского языков.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List


# ── Паттерн для токенизации слов (кириллица + латиница + дефис внутри) ──────

_RE_WORD = re.compile(
    r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+(?:[-'][А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+)*"
)

# ── Пороги категорий темпа речи (WPM) ───────────────────────────────────────

_PACE_SLOW_MAX = 100.0       # < 100 wpm → "slow"
_PACE_NORMAL_MAX = 160.0     # 100–160 wpm → "normal"
_PACE_FAST_MAX = 200.0       # 160–200 wpm → "fast"
#                              > 200 wpm → "very_fast"

# Эталонная скорость чтения для расчёта estimated_reading_time_sec
_READING_WPM = 150.0


def _tokenize_words(text: str) -> List[str]:
    """Возвращает список слов из текста (кириллица + латиница)."""
    return _RE_WORD.findall(text)


def _pace_category(wpm: float) -> str:
    """Возвращает строковую категорию темпа речи по WPM.

    «slow»      — < 100 wpm
    «normal»    — 100–160 wpm
    «fast»      — 160–200 wpm
    «very_fast» — > 200 wpm
    """
    if wpm < _PACE_SLOW_MAX:
        return "slow"
    if wpm <= _PACE_NORMAL_MAX:
        return "normal"
    if wpm <= _PACE_FAST_MAX:
        return "fast"
    return "very_fast"


@dataclass
class PaceReport:
    """Результат анализа темпа речи."""

    words_per_minute: float           # слов в минуту
    chars_per_minute: float           # символов в минуту
    pace_category: str                # "slow" | "normal" | "fast" | "very_fast"
    estimated_reading_time_sec: float  # расчётное время чтения при 150 wpm
    word_count: int                   # число слов
    char_count: int                   # число символов (без пробелов)
    duration_sec: float               # фактическая длительность записи

    def as_dict(self) -> dict:
        """Сериализует отчёт в словарь для IPC-ответа."""
        return asdict(self)


class SpeechPaceAnalyzer:
    """Анализирует темп речи по тексту и длительности аудио.

    Не требует внешних библиотек. Поддерживает русский, испанский и
    английский тексты.

    Пример использования::

        analyzer = SpeechPaceAnalyzer()
        report = analyzer.analyze("Привет, как дела?", duration_sec=3.0)
        print(report.words_per_minute, report.pace_category)
    """

    def analyze(self, text: str, duration_sec: float) -> PaceReport:
        """Анализирует темп речи и возвращает PaceReport.

        Args:
            text: транскрибированный текст.
            duration_sec: фактическая длительность аудиозаписи в секундах.
                          При <= 0 возвращает нулевой отчёт.

        Returns:
            PaceReport с рассчитанными метриками темпа.
        """
        if not text or not text.strip() or duration_sec <= 0:
            return self._empty_report(duration_sec=max(0.0, duration_sec))

        words = _tokenize_words(text)
        word_count = len(words)

        if word_count == 0:
            return self._empty_report(duration_sec=duration_sec)

        # Символы без пробелов (только буквы слов)
        char_count = sum(len(w) for w in words)

        minutes = duration_sec / 60.0
        wpm = word_count / minutes
        cpm = char_count / minutes

        # Расчётное время чтения при 150 wpm (в секундах)
        reading_time_sec = (word_count / _READING_WPM) * 60.0

        return PaceReport(
            words_per_minute=round(wpm, 2),
            chars_per_minute=round(cpm, 2),
            pace_category=_pace_category(wpm),
            estimated_reading_time_sec=round(reading_time_sec, 2),
            word_count=word_count,
            char_count=char_count,
            duration_sec=round(duration_sec, 3),
        )

    def compare_pace(self, reports: List[PaceReport]) -> dict:
        """Сравнивает несколько PaceReport и возвращает агрегированную статистику.

        Возвращает avg/min/max по wpm, cpm и duration_sec, а также
        распределение по категориям темпа.

        Args:
            reports: список PaceReport (может быть пустым).

        Returns:
            Словарь со статистикой. При пустом списке все значения — 0.
        """
        if not reports:
            return {
                "count": 0,
                "wpm": {"avg": 0.0, "min": 0.0, "max": 0.0},
                "cpm": {"avg": 0.0, "min": 0.0, "max": 0.0},
                "duration_sec": {"avg": 0.0, "min": 0.0, "max": 0.0},
                "pace_distribution": {
                    "slow": 0,
                    "normal": 0,
                    "fast": 0,
                    "very_fast": 0,
                },
            }

        wpms = [r.words_per_minute for r in reports]
        cpms = [r.chars_per_minute for r in reports]
        durations = [r.duration_sec for r in reports]
        n = len(reports)

        distribution: dict[str, int] = {"slow": 0, "normal": 0, "fast": 0, "very_fast": 0}
        for r in reports:
            distribution[r.pace_category] = distribution.get(r.pace_category, 0) + 1

        return {
            "count": n,
            "wpm": {
                "avg": round(sum(wpms) / n, 2),
                "min": round(min(wpms), 2),
                "max": round(max(wpms), 2),
            },
            "cpm": {
                "avg": round(sum(cpms) / n, 2),
                "min": round(min(cpms), 2),
                "max": round(max(cpms), 2),
            },
            "duration_sec": {
                "avg": round(sum(durations) / n, 3),
                "min": round(min(durations), 3),
                "max": round(max(durations), 3),
            },
            "pace_distribution": distribution,
        }

    # ── Вспомогательные методы ───────────────────────────────────────────────

    @staticmethod
    def _empty_report(duration_sec: float = 0.0) -> PaceReport:
        """Возвращает нулевой PaceReport для пустого/невалидного ввода."""
        return PaceReport(
            words_per_minute=0.0,
            chars_per_minute=0.0,
            pace_category="slow",
            estimated_reading_time_sec=0.0,
            word_count=0,
            char_count=0,
            duration_sec=duration_sec,
        )
