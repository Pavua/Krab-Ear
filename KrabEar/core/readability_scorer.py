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

# Паттерн для разбивки на предложения (основной — после ! и ?)
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[!?])\s+(?=[A-ZА-ЯЁ\"À-ɏ])")
# Паттерн для разбивки на предложения по точке (только после слов ≥2 символа,
# чтобы не срабатывать на однобуквенные аббревиатуры вида «г.», «ул.»)
_RE_SENTENCE_SPLIT_DOT = re.compile(
    r"(?<=[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]{2}\.)\s+(?=[A-ZА-ЯЁ\"À-ɏ])"
)
# Паттерн для токенизации слов (кириллица + латиница + дефис внутри)
_RE_WORD = re.compile(r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+(?:[-'][А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+)*")
# Паттерн для очистки дефисов/апострофов при подсчёте длины слова
_RE_HYPHEN_APOS = re.compile(r"[-']")

# Заглушка для троеточия при разбивке на предложения
_ELLIPSIS_PLACEHOLDER = "\x00ELLIPSIS\x00"

# Известные русские сокращения (без точки), которые не должны делить предложение.
# Например: «г.» (год/город), «ул.» (улица), «т.е.» (то есть), «т.д.» (так далее),
# «т.п.» (тому подобное), «стр.» (страница), «ч.» (часть).
_RU_ABBREV = frozenset({"г", "ул", "т.е", "т.д", "т.п", "стр", "ч", "пр", "д", "кв"})

# Паттерн для замены известных аббревиатур перед разбивкой
_RE_ABBREV = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(_RU_ABBREV, key=len, reverse=True)) + r")\."
)


def _count_syllables(word: str) -> int:
    """Считает слоги в слове по числу гласных (эвристика для RU/ES/EN)."""
    count = sum(1 for ch in word if ch in _ALL_VOWELS)
    return max(1, count)


def _tokenize_words(text: str) -> List[str]:
    """Возвращает список слов из текста."""
    return _RE_WORD.findall(text)


def _split_sentences(text: str) -> List[str]:
    """Разбивает текст на предложения, фильтруя пустые.

    Корректно обрабатывает:
    - Троеточие ``...`` — не считается границей предложения.
    - Русские сокращения ``г.``, ``ул.``, ``т.е.``, ``т.д.``, ``т.п.`` и др.
    - Разрыв предложения только по ``.`` перед заглавной буквой, если перед
      точкой стоит слово длиной ≥ 2 символа (исключает однобуквенные аббревиатуры).
    """
    text = text.strip()
    if not text:
        return []

    # 1. Заменяем троеточие-символ и буквенное «...» на заглушку
    work = text.replace("…", _ELLIPSIS_PLACEHOLDER)
    work = work.replace("...", _ELLIPSIS_PLACEHOLDER)

    # 2. Защищаем известные сокращения: «г.» → «г\x00»  (убираем точку)
    #    Потом восстановим при финальной замене заглушек.
    _ABBREV_MARK = "\x00ABBREV\x00"

    def _protect_abbrev(m: re.Match) -> str:
        return m.group(1) + _ABBREV_MARK

    work = _RE_ABBREV.sub(_protect_abbrev, work)

    # 3. Разбиваем по ! и ?
    parts: List[str] = []
    current_parts = _RE_SENTENCE_SPLIT.split(work)
    for part in current_parts:
        # Дополнительно разбиваем по точке (только перед заглавной, слово ≥ 2 символа)
        sub_parts = _RE_SENTENCE_SPLIT_DOT.split(part)
        parts.extend(sub_parts)

    # 4. Восстанавливаем заглушки
    def _restore(s: str) -> str:
        s = s.replace(_ELLIPSIS_PLACEHOLDER, "...")
        s = s.replace(_ABBREV_MARK, ".")
        return s.strip()

    sentences = [_restore(s) for s in parts if _restore(s)]

    if not sentences:
        return [text] if text else []
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
