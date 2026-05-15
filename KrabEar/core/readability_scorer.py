"""readability_scorer.py — оценка читабельности транскрибаций Krab Ear.

Вычисляет метрики сложности текста без внешних зависимостей.
Адаптировано для русского и испанского языков.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


# ── Гласные для подсчёта слогов ─────────────────────────────────────────────

_VOWELS_RU = frozenset("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
_VOWELS_ES = frozenset("aeiouáéíóúüAEIOUÁÉÍÓÚÜ")
_VOWELS_EN = frozenset("aeiouAEIOU")
_ALL_VOWELS = _VOWELS_RU | _VOWELS_ES | _VOWELS_EN

# Паттерн для разбивки на предложения
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
# Паттерн для токенизации слов (кириллица + латиница + дефис внутри)
_RE_WORD = re.compile(r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+(?:[-'][А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+)*")
# Паттерн для очистки дефисов/апострофов при подсчёте длины слова
_RE_HYPHEN_APOS = re.compile(r"[-']")


def _count_syllables(word: str) -> int:
    """Считает слоги в слове по числу гласных (эвристика для RU/ES/EN)."""
    count = sum(1 for ch in word if ch in _ALL_VOWELS)
    return max(1, count)


def _tokenize_words(text: str) -> List[str]:
    """Возвращает список слов из текста."""
    return _RE_WORD.findall(text)


def _split_sentences(text: str) -> List[str]:
    """Разбивает текст на предложения, фильтруя пустые."""
    raw = _RE_SENTENCE_SPLIT.split(text.strip())
    sentences = [s.strip() for s in raw if s.strip()]
    # Если нет явных разделителей — весь текст одно предложение
    if not sentences:
        return [text.strip()] if text.strip() else []
    return sentences


def _vocabulary_level(avg_word_length: float, flesch: float) -> str:
    """Определяет уровень словарного запаса по средней длине слова и оценке Флеша.

    Пороги подобраны для русского языка, где слова в среднем длиннее английских.
    """
    if avg_word_length <= 4.5 and flesch >= 60:
        return "simple"
    if avg_word_length >= 7.0 or flesch < 30:
        return "complex"
    return "moderate"


@dataclass
class ReadabilityReport:
    """Результат анализа читабельности текста."""
    flesch_score: float          # адаптированный индекс Флеша (0–100, выше = легче)
    avg_sentence_length: float   # среднее число слов в предложении
    avg_word_length: float       # средняя длина слова в символах
    vocabulary_level: str        # "simple" | "moderate" | "complex"
    sentence_count: int          # число предложений
    word_count: int              # число слов
    longest_sentence: str        # самое длинное предложение
    shortest_sentence: str       # самое короткое предложение


class ReadabilityScorer:
    """Оценивает читабельность транскрибированного текста.

    Адаптирован для русского (основной язык Krab Ear), испанского и английского.
    Не требует внешних библиотек.

    Пример использования::

        scorer = ReadabilityScorer()
        report = scorer.score("Привет мир. Это простой текст.")
        print(report.flesch_score, report.vocabulary_level)
    """

    # Коэффициенты формулы Флеша, адаптированные для RU/ES
    # Flesch = 206.835 - 1.015 * ASL - 84.6 * ASW  (оригинал EN)
    # Для русского и испанского слова длиннее → корректируем коэффициент слогов
    _FLESCH_BASE = 206.835
    _FLESCH_ASL_COEFF = 1.015   # коэффициент средней длины предложения
    _FLESCH_ASW_COEFF = 60.0    # коэффициент среднего числа слогов на слово (снижен для RU)

    def score(self, text: str) -> ReadabilityReport:
        """Анализирует текст и возвращает ReadabilityReport.

        Args:
            text: произвольный текст (транскрибация, заметка и т.п.).

        Returns:
            ReadabilityReport с рассчитанными метриками.
            При пустом или однословном тексте возвращает нейтральный отчёт.
        """
        if not text or not text.strip():
            return self._empty_report()

        sentences = _split_sentences(text)
        all_words = _tokenize_words(text)

        word_count = len(all_words)
        sentence_count = len(sentences)

        if word_count == 0:
            return self._empty_report()

        # Средняя длина предложения (в словах)
        avg_sentence_length = word_count / sentence_count if sentence_count else float(word_count)

        # Средняя длина слова (в символах, без учёта дефисов)
        char_lengths = [len(_RE_HYPHEN_APOS.sub("", w)) for w in all_words]
        avg_word_length = sum(char_lengths) / word_count if word_count else 0.0

        # Среднее число слогов на слово
        total_syllables = sum(_count_syllables(w) for w in all_words)
        avg_syllables_per_word = total_syllables / word_count if word_count else 1.0

        # Формула Флеша (адаптирована)
        flesch = (
            self._FLESCH_BASE
            - self._FLESCH_ASL_COEFF * avg_sentence_length
            - self._FLESCH_ASW_COEFF * avg_syllables_per_word
        )
        flesch = max(0.0, min(100.0, flesch))

        # Longest / shortest sentence (по числу слов)
        sentences_with_counts = [
            (s, len(_tokenize_words(s))) for s in sentences
        ]
        longest_sentence = max(sentences_with_counts, key=lambda x: x[1])[0]
        shortest_sentence = min(sentences_with_counts, key=lambda x: x[1])[0]

        vocab_level = _vocabulary_level(avg_word_length, flesch)

        return ReadabilityReport(
            flesch_score=round(flesch, 2),
            avg_sentence_length=round(avg_sentence_length, 2),
            avg_word_length=round(avg_word_length, 2),
            vocabulary_level=vocab_level,
            sentence_count=sentence_count,
            word_count=word_count,
            longest_sentence=longest_sentence,
            shortest_sentence=shortest_sentence,
        )

    # ── Вспомогательные методы ───────────────────────────────────────────────

    @staticmethod
    def _empty_report() -> ReadabilityReport:
        """Возвращает нейтральный отчёт для пустого/невалидного текста."""
        return ReadabilityReport(
            flesch_score=0.0,
            avg_sentence_length=0.0,
            avg_word_length=0.0,
            vocabulary_level="simple",
            sentence_count=0,
            word_count=0,
            longest_sentence="",
            shortest_sentence="",
        )
