"""term_extractor.py — интеллектуальное извлечение терминов из транскрибаций.

Используется для авто-обучения глоссария и словаря STT.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Dict

from core.stop_words import StopWords

logger = logging.getLogger(__name__)

# ── Слова специфичные для TermExtractor, отсутствующие в stop_words.py ───────
# TODO(W1095): влить эти слова в core/stop_words.py в рамках следующего
# планового обновления стоп-списков (RU: формы который/один/другой/мочь/мне/свой
# ES: puede/cada/solo/bueno/vale/mismos и др.)
#
# RU-специфичные (не покрыты stop_words._RU):
_EXTRA_STOP_RU: frozenset = frozenset({
    "ваша", "ваши", "всем", "всех",
    "давай", "давайте",
    "другие", "другой",
    "которая", "которого", "которое", "которой", "которые", "который",
    "мне",
    "могут", "можем", "может",
    "наша", "наши",
    "некоторые", "ничего",
    "нужна",
    "один", "одна", "одни", "одно",
    "самая", "самое", "самый",
    "своего", "своей", "свои", "свою",
    "такое",
    "у",
    "этом", "этому", "эту",
})

# ES-специфичные (не покрыты stop_words._ES):
_EXTRA_STOP_ES: frozenset = frozenset({
    "bueno", "cada", "entonces",
    "hace", "hacen",
    "misma", "mismo", "mismos",
    "mucha", "muchas", "muchos",
    "otra", "otras", "otro", "otros",
    "podemos", "puede", "pueden",
    "sido", "solo",
    "tienen", "toda", "uno", "vale",
})

# Предупреждение логируется один раз при загрузке модуля
logger.warning(
    "term_extractor: using %d RU + %d ES extra stop-words not yet in "
    "core/stop_words.py — TODO(W1095): merge into stop_words.py",
    len(_EXTRA_STOP_RU),
    len(_EXTRA_STOP_ES),
)

# Объединённые стоп-слова из unified StopWords + специфичные для TermExtractor
_ALL_STOP_WORDS: frozenset = (
    StopWords.get_stop_words("ru")
    | StopWords.get_stop_words("es")
    | StopWords.get_stop_words("en")
    | _EXTRA_STOP_RU
    | _EXTRA_STOP_ES
)


@dataclass
class ExtractedTerm:
    """Извлечённый термин с метаданными."""
    term: str
    frequency: int
    is_proper_noun: bool
    context: str          # первый фрагмент текста, где встретился термин
    confidence: float     # 0.0–1.0


# ── Вспомогательные regex-паттерны ──────────────────────────────────────────

# Заглавные слова (русские и латинские)
_RE_CAPITALIZED = re.compile(r"(?<!\A)(?<!\. )(?<!\n)\b([А-ЯA-Z][А-Яа-яA-Za-z]{2,})\b")
# CamelCase (минимум две части)
_RE_CAMEL_CASE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")
# Слова с цифрами (технические термины: GPT-4, iPhone13, H2O)
# W1769 ReDoS-fix: вторая ветка раньше имела перекрывающийся жадный класс
# `[A-Za-zА-Яа-я0-9\-]*` прямо перед `[0-9]+` → O(n²) backtracking на чисто-
# цифровом токене. Переписано с обязательной буквой после серии цифр (без
# ведущего перекрытия по `[0-9]`); набор совпадений на реальных токенах не изменился.
_RE_TECH_WITH_DIGITS = re.compile(r"\b([A-Za-zА-Яа-я]+[0-9]+[A-Za-zА-Яа-я0-9\-]*|[A-Za-zА-Яа-я]*[0-9]+[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9\-]*)\b")
# Аббревиатуры (2+ заглавных букв латиница/кириллица)
_RE_ABBREV = re.compile(r"\b([A-ZА-Я]{2,})\b")
# Слова для биграмм/триграмм
_RE_WORD = re.compile(r"[А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñÜü]{3,}")
# Разбиение на предложения по знакам конца (lookbehind)
_RE_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Очистка слова от не-буквенных кириллических/unicode символов
_RE_WORD_CLEAN = re.compile(r"[^\wА-Яа-я]")


def _sentences(text: str) -> list[str]:
    """Разбивает текст на предложения."""
    return _RE_SENT_SPLIT.split(text.strip())


def _is_stop_word(word: str) -> bool:
    return word.lower() in _ALL_STOP_WORDS


def _context_snippet(text: str, term: str, max_len: int = 80) -> str:
    """Возвращает короткий фрагмент текста вокруг первого вхождения термина."""
    idx = text.find(term)
    if idx == -1:
        return text[:max_len]
    start = max(0, idx - 20)
    end = min(len(text), idx + len(term) + 40)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


class TermExtractor:
    """Извлекает термины-кандидаты из транскрибированных текстов.

    Стратегии:
    - Заглавные слова в середине предложения → proper nouns
    - CamelCase, слова-с-цифрами, аббревиатуры → технические термины
    - Повторяющиеся биграммы/триграммы (≥3 раза) → составные термины
    - Фильтрация стоп-слов и слишком коротких слов
    """

    def __init__(self, min_term_length: int = 3) -> None:
        self.min_term_length = min_term_length

    # ── Публичный API ────────────────────────────────────────────────────────

    def extract_terms(self, text: str, language: str = "ru") -> List[ExtractedTerm]:
        """Извлекает термины из одного текста.

        Args:
            text: входной текст (транскрибация).
            language: подсказка о языке ("ru", "es", "en") — влияет на набор
                      стоп-слов и порог уверенности.
        Returns:
            Список ExtractedTerm, отсортированный по убыванию confidence.
        """
        if not text or not text.strip():
            return []

        results: Dict[str, ExtractedTerm] = {}

        # 1. Proper nouns — заглавные слова в середине предложения
        for sent in _sentences(text):
            # Пропускаем первое слово предложения (всегда с заглавной)
            words_in_sent = sent.split()
            for i, word in enumerate(words_in_sent):
                if i == 0:
                    continue
                clean = _RE_WORD_CLEAN.sub("", word)
                if not clean or len(clean) < self.min_term_length:
                    continue
                if _RE_CAPITALIZED.match(clean) and not _is_stop_word(clean):
                    key = clean.lower()
                    if key not in results:
                        results[key] = ExtractedTerm(
                            term=clean,
                            frequency=1,
                            is_proper_noun=True,
                            context=_context_snippet(text, clean),
                            confidence=0.75,
                        )
                    else:
                        results[key].frequency += 1

        # 2. CamelCase
        for m in _RE_CAMEL_CASE.finditer(text):
            term = m.group(1)
            key = term.lower()
            if key not in results:
                results[key] = ExtractedTerm(
                    term=term,
                    frequency=1,
                    is_proper_noun=False,
                    context=_context_snippet(text, term),
                    confidence=0.8,
                )
            else:
                results[key].frequency += 1

        # 3. Технические слова с цифрами
        for m in _RE_TECH_WITH_DIGITS.finditer(text):
            term = m.group(1)
            if len(term) < self.min_term_length:
                continue
            key = term.lower()
            if key not in results:
                results[key] = ExtractedTerm(
                    term=term,
                    frequency=1,
                    is_proper_noun=False,
                    context=_context_snippet(text, term),
                    confidence=0.85,
                )
            else:
                results[key].frequency += 1

        # 4. Аббревиатуры (≥2 заглавных)
        for m in _RE_ABBREV.finditer(text):
            term = m.group(1)
            if len(term) < 2:
                continue
            key = term.lower()
            if key not in results:
                results[key] = ExtractedTerm(
                    term=term,
                    frequency=1,
                    is_proper_noun=False,
                    context=_context_snippet(text, term),
                    confidence=0.7,
                )
            else:
                results[key].frequency += 1

        # 5. Повторяющиеся биграммы
        bigram_terms = self._extract_repeated_ngrams(text, n=2, min_freq=2)
        for term, freq in bigram_terms.items():
            key = term.lower()
            if key not in results:
                results[key] = ExtractedTerm(
                    term=term,
                    frequency=freq,
                    is_proper_noun=False,
                    context=_context_snippet(text, term),
                    confidence=0.65,
                )

        # Нормализуем confidence с учётом частоты
        for et in results.values():
            freq_bonus = min(0.15, et.frequency * 0.03)
            et.confidence = min(1.0, et.confidence + freq_bonus)

        return sorted(results.values(), key=lambda t: (-t.confidence, -t.frequency, t.term))

    def extract_from_history(
        self,
        items: list,
        min_frequency: int = 3,
    ) -> List[ExtractedTerm]:
        """Извлекает термины из списка записей истории транскрибаций.

        Args:
            items: список dict-записей истории (поля: text, source_text).
            min_frequency: минимальная суммарная частота по всем записям.
        Returns:
            Список ExtractedTerm, отфильтрованный по min_frequency.
        """
        # Агрегируем частоты и контексты по всем текстам истории
        freq: Counter = Counter()
        meta: Dict[str, dict] = {}  # term_key → {is_proper_noun, context, confidence}

        for item in items:
            raw = str(item.get("source_text", "") or item.get("text", "") or "")
            if not raw.strip():
                continue
            for et in self.extract_terms(raw):
                key = et.term.lower()
                freq[key] += et.frequency
                if key not in meta or et.confidence > meta[key]["confidence"]:
                    meta[key] = {
                        "term": et.term,
                        "is_proper_noun": et.is_proper_noun,
                        "context": et.context,
                        "confidence": et.confidence,
                    }

        results: List[ExtractedTerm] = []
        for key, total_freq in freq.items():
            if total_freq < min_frequency:
                continue
            m = meta[key]
            freq_bonus = min(0.15, total_freq * 0.02)
            confidence = min(1.0, m["confidence"] + freq_bonus)
            results.append(ExtractedTerm(
                term=m["term"],
                frequency=total_freq,
                is_proper_noun=m["is_proper_noun"],
                context=m["context"],
                confidence=confidence,
            ))

        return sorted(results, key=lambda t: (-t.confidence, -t.frequency, t.term))

    # ── Вспомогательные методы ───────────────────────────────────────────────

    def _extract_repeated_ngrams(
        self, text: str, n: int = 2, min_freq: int = 2
    ) -> Dict[str, int]:
        """Находит n-граммы, встречающиеся не менее min_freq раз."""
        words = _RE_WORD.findall(text)
        # Фильтруем стоп-слова из позиций
        filtered = [w for w in words if not _is_stop_word(w) and len(w) >= self.min_term_length]
        if len(filtered) < n:
            return {}

        ngram_counts: Counter = Counter()
        for i in range(len(filtered) - n + 1):
            gram = " ".join(filtered[i: i + n])
            ngram_counts[gram] += 1

        return {gram: cnt for gram, cnt in ngram_counts.items() if cnt >= min_freq}
