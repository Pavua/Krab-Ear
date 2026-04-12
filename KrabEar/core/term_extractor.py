"""term_extractor.py — интеллектуальное извлечение терминов из транскрибаций.

Используется для авто-обучения глоссария и словаря STT.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import List, Dict


# ── Стоп-слова ──────────────────────────────────────────────────────────────

_STOP_WORDS_RU: frozenset = frozenset([
    "быть", "было", "была", "были", "буду", "будет", "будут",
    "этот", "этой", "этом", "этих", "этого", "этому", "эту",
    "который", "которая", "которое", "которые", "которого", "которой",
    "может", "можно", "могут", "можем",
    "если", "когда", "потом", "потому", "после", "перед",
    "очень", "более", "менее", "также", "тоже",
    "через", "между", "около", "вокруг",
    "нужно", "нужна", "надо", "просто",
    "здесь", "сейчас", "тогда", "всегда", "никогда",
    "ничего", "некоторые", "каждый", "другой", "другие",
    "такой", "такая", "такие", "такое",
    "свой", "свою", "свои", "своей", "своего",
    "весь", "вся", "всё", "все", "всех", "всем",
    "один", "одна", "одно", "одни",
    "наш", "наша", "наши", "ваш", "ваша", "ваши",
    "есть", "нет", "там", "тут", "еще", "ещё", "уже",
    "только", "самый", "самая", "самое",
    "хорошо", "ладно", "давай", "давайте",
    "это", "при", "или", "для", "как", "что", "так", "его", "её", "их",
    "они", "она", "оно", "мне", "мы", "вы", "он", "но", "от", "до",
    "по", "из", "на", "в", "с", "к", "о", "у", "за", "не", "и", "а",
    "то", "да", "же", "бы", "был",
])

_STOP_WORDS_ES: frozenset = frozenset([
    "pero", "para", "como", "desde", "este", "esta", "esto",
    "estos", "estas", "donde", "cuando", "porque", "aunque",
    "puede", "pueden", "podemos", "tiene", "tienen",
    "hace", "hacen", "está", "están", "sido", "haber",
    "también", "mucho", "mucha", "muchos", "muchas",
    "otro", "otra", "otros", "otras",
    "todo", "toda", "todos", "todas",
    "cada", "mismo", "misma", "mismos",
    "algo", "nada", "siempre", "nunca",
    "aquí", "ahora", "entonces", "después", "antes",
    "entre", "sobre", "contra", "hacia",
    "solo", "bueno", "bien", "vale",
    "que", "del", "los", "las", "por", "con", "una", "uno",
    "muy", "más", "sin", "nos", "fue", "ser", "hay",
])

_STOP_WORDS_EN: frozenset = frozenset([
    "the", "and", "that", "this", "with", "from", "have", "been",
    "will", "would", "could", "should", "which", "their", "there",
    "they", "them", "then", "than", "when", "what", "where", "who",
    "how", "but", "for", "not", "are", "was", "were", "has", "had",
    "its", "our", "your", "his", "her", "its", "can", "may",
])

_ALL_STOP_WORDS = _STOP_WORDS_RU | _STOP_WORDS_ES | _STOP_WORDS_EN


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
_RE_TECH_WITH_DIGITS = re.compile(r"\b([A-Za-zА-Яа-я]+[0-9]+[A-Za-zА-Яа-я0-9\-]*|[A-Za-zА-Яа-я0-9\-]*[0-9]+[A-Za-zА-Яа-я]+)\b")
# Аббревиатуры (2+ заглавных букв латиница/кириллица)
_RE_ABBREV = re.compile(r"\b([A-ZА-Я]{2,})\b")
# Слова для биграмм/триграмм
_RE_WORD = re.compile(r"[А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñÜü]{3,}")


def _sentences(text: str) -> list[str]:
    """Разбивает текст на предложения."""
    return re.split(r"(?<=[.!?])\s+", text.strip())


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
                clean = re.sub(r"[^\wА-Яа-я]", "", word, flags=re.UNICODE)
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
