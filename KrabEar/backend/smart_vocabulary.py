"""smart_vocabulary.py — умный построитель словаря STT из паттернов использования.

Анализирует историю транскрибаций и автоматически строит/обновляет
пользовательский словарь для подсказок Whisper.

Стратегии извлечения:
- Имена собственные (заглавные слова в середине предложения)
- Технические термины (CamelCase, слова с цифрами, аббревиатуры)
- Часто неправильно распознаваемые слова (низкий confidence → повторяющиеся слова)
- Доменно-специфические термины из последних контекстов
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.term_extractor import TermExtractor, _is_stop_word

logger = logging.getLogger("KrabEar.Backend.SmartVocabulary")

# ── Стоп-слова для быстрой фильтрации ─────────────────────────────────────────

_COMMON_STOP_WORDS: frozenset = frozenset([
    # RU
    "быть", "было", "была", "были", "этот", "этой", "этом",
    "который", "которая", "которые", "может", "можно", "если",
    "когда", "потом", "очень", "более", "также", "через",
    "нужно", "надо", "просто", "здесь", "сейчас", "тогда",
    "ничего", "каждый", "другой", "такой", "такая", "весь",
    "один", "одна", "наш", "ваш", "есть", "нет", "там",
    "только", "самый", "хорошо", "ладно", "давай", "давайте",
    "это", "при", "или", "для", "как", "что", "так", "его",
    "они", "она", "оно", "мне", "мы", "вы", "он", "но",
    "по", "из", "на", "в", "с", "к", "о", "у", "за", "не",
    "и", "а", "то", "да", "же", "бы", "был", "там", "уже",
    # ES
    "pero", "para", "como", "este", "esta", "esto", "donde",
    "cuando", "porque", "puede", "tiene", "hace", "está",
    "también", "mucho", "todo", "cada", "algo", "nada",
    "solo", "bueno", "bien", "que", "del", "los", "las",
    "por", "con", "una", "uno", "muy", "más", "sin",
    # EN
    "the", "and", "that", "this", "with", "from", "have",
    "will", "would", "could", "should", "which", "their",
    "they", "them", "then", "than", "when", "what", "where",
    "but", "for", "not", "are", "was", "were", "has", "had",
    "its", "our", "your", "his", "her", "can", "may",
])

# Порог confidence ниже которого запись считается «ненадёжной»
_LOW_CONFIDENCE_THRESHOLD = 0.65

# Regex для слов (поддержка кириллицы и латиницы)
_RE_WORD = re.compile(r"[А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñÜü]{3,}")
_RE_CAMEL_CASE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")
_RE_TECH_WITH_DIGITS = re.compile(
    r"\b([A-Za-zА-Яа-я]+[0-9]+[A-Za-zА-Яа-я0-9\-]*"
    r"|[A-Za-zА-Яа-я0-9\-]*[0-9]+[A-Za-zА-Яа-я]+)\b"
)
_RE_ABBREV = re.compile(r"\b([A-ZА-Я]{2,})\b")
_RE_CAPITALIZED_MID = re.compile(
    r"(?<![.!?\n])(?<=\s)([А-ЯA-Z][А-Яа-яA-Za-z]{2,})\b"
)


# ── Датаклассы ─────────────────────────────────────────────────────────────────


@dataclass
class VocabularyUpdate:
    """Результат одного цикла обновления словаря."""
    new_words: List[str] = field(default_factory=list)
    removed_words: List[str] = field(default_factory=list)
    total: int = 0
    sources: Dict[str, int] = field(default_factory=dict)
    """sources: dict с ключами-стратегиями → кол-во слов из этой стратегии."""


# ── Основной класс ─────────────────────────────────────────────────────────────


class SmartVocabularyBuilder:
    """Умный построитель словаря STT на основе паттернов использования.

    Анализирует список записей истории и строит обновление словаря Whisper,
    объединяя несколько стратегий извлечения.

    Args:
        min_word_length: минимальная длина слова для включения в словарь.
    """

    def __init__(self, min_word_length: int = 3) -> None:
        self.min_word_length = min_word_length
        self._extractor = TermExtractor(min_term_length=min_word_length)

    # ── Публичный API ──────────────────────────────────────────────────────────

    def build_vocabulary(
        self,
        items: List[Dict[str, Any]],
        min_frequency: int = 3,
    ) -> VocabularyUpdate:
        """Строит обновление словаря из истории транскрибаций.

        Args:
            items: записи истории — list[dict] с полями text, source_text,
                   confidence (опционально).
            min_frequency: минимальная частота слова для включения.

        Returns:
            VocabularyUpdate с новыми словами и метрикой по источникам.
        """
        if not items:
            return VocabularyUpdate()

        sources: Dict[str, List[str]] = {
            "proper_nouns": [],
            "technical_terms": [],
            "misrecognized": [],
            "domain_terms": [],
        }

        # 1. Proper nouns + technical terms через TermExtractor
        extracted = self._extractor.extract_from_history(items, min_frequency=min_frequency)
        for et in extracted:
            if len(et.term) < self.min_word_length:
                continue
            if _is_stop_word(et.term):
                continue
            if et.is_proper_noun:
                sources["proper_nouns"].append(et.term)
            else:
                sources["technical_terms"].append(et.term)

        # 2. Слова из низко-уверенных записей (часто неправильно распознаваемые)
        low_conf_words = self._extract_misrecognized_words(items, min_frequency=min_frequency)
        sources["misrecognized"].extend(low_conf_words)

        # 3. Доменно-специфические термины из последних контекстов
        domain_words = self._extract_domain_terms(items, min_frequency=min_frequency)
        sources["domain_terms"].extend(domain_words)

        # Объединяем, дедуплицируем
        seen: set = set()
        all_new: List[str] = []
        for src_words in sources.values():
            for w in src_words:
                key = w.strip()
                if not key or key.lower() in seen:
                    continue
                seen.add(key.lower())
                all_new.append(key)

        all_new.sort()

        return VocabularyUpdate(
            new_words=all_new,
            removed_words=[],
            total=len(all_new),
            sources={k: len(v) for k, v in sources.items()},
        )

    def auto_update(
        self,
        store: Any,
        vocabulary_store: Any,
        min_frequency: int = 3,
        scan_limit: int = 200,
    ) -> VocabularyUpdate:
        """Полный цикл авто-обновления: читает историю → строит словарь → сохраняет.

        Args:
            store: StateStore (имеет get_history_page).
            vocabulary_store: VocabularyStore (имеет load/add_words/remove_words).
            min_frequency: минимальная частота для включения.
            scan_limit: кол-во последних записей для анализа.

        Returns:
            VocabularyUpdate с реально добавленными/удалёнными словами.
        """
        # Загружаем историю
        try:
            items, _ = store.get_history_page(cursor=None, limit=scan_limit)
        except Exception as exc:
            logger.error("auto_update: ошибка загрузки истории: %s", exc)
            return VocabularyUpdate()

        if not items:
            return VocabularyUpdate()

        # Конвертируем объекты в dict если нужно
        raw_items = [i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in items]

        # Строим обновление словаря
        update = self.build_vocabulary(raw_items, min_frequency=min_frequency)

        if not update.new_words:
            logger.info("auto_update: нет новых слов для добавления")
            return update

        # Загружаем текущий словарь, фильтруем уже существующие
        try:
            existing = set(vocabulary_store.load())
        except Exception as exc:
            logger.error("auto_update: ошибка загрузки словаря: %s", exc)
            existing = set()

        truly_new = [w for w in update.new_words if w not in existing]
        if not truly_new:
            logger.info("auto_update: все слова уже в словаре")
            return VocabularyUpdate(new_words=[], total=len(existing), sources=update.sources)

        # Сохраняем
        try:
            vocabulary_store.add_words(truly_new)
            logger.info("auto_update: добавлено %d слов в словарь", len(truly_new))
        except Exception as exc:
            logger.error("auto_update: ошибка сохранения словаря: %s", exc)

        return VocabularyUpdate(
            new_words=truly_new,
            removed_words=[],
            total=len(existing) + len(truly_new),
            sources=update.sources,
        )

    def get_vocabulary_suggestions(
        self,
        items: List[Dict[str, Any]],
        existing: Optional[List[str]] = None,
        min_frequency: int = 2,
        top_k: int = 30,
    ) -> List[Dict[str, Any]]:
        """Предлагает новые слова для словаря без сохранения.

        Args:
            items: записи истории.
            existing: уже известные слова (не предлагать повторно).
            min_frequency: минимальная частота слова.
            top_k: максимальное количество предложений.

        Returns:
            list[dict] с ключами: word, frequency, source, confidence.
        """
        if not items:
            return []

        existing_lower = {w.lower() for w in (existing or [])}
        suggestions: Dict[str, Dict[str, Any]] = {}

        # Счётчики частоты по всем текстам
        word_freq: Counter = Counter()
        word_source: Dict[str, str] = {}
        word_confidence: Dict[str, float] = {}

        for item in items:
            raw_text = str(item.get("source_text", "") or item.get("text", "") or "")
            if not raw_text.strip():
                continue
            item_conf = float(item.get("confidence", 1.0) or 1.0)

            # Proper nouns
            for m in _RE_CAPITALIZED_MID.finditer(raw_text):
                word = m.group(1)
                if len(word) >= self.min_word_length and not _is_stop_word(word):
                    key = word.lower()
                    word_freq[key] += 1
                    if key not in word_source:
                        word_source[key] = "proper_noun"
                        word_confidence[key] = item_conf

            # CamelCase
            for m in _RE_CAMEL_CASE.finditer(raw_text):
                word = m.group(1)
                key = word.lower()
                word_freq[key] += 1
                if key not in word_source:
                    word_source[key] = "camelcase"
                    word_confidence[key] = 0.8

            # Технические слова с цифрами
            for m in _RE_TECH_WITH_DIGITS.finditer(raw_text):
                word = m.group(1)
                if len(word) >= self.min_word_length:
                    key = word.lower()
                    word_freq[key] += 1
                    if key not in word_source:
                        word_source[key] = "technical"
                        word_confidence[key] = 0.85

            # Аббревиатуры
            for m in _RE_ABBREV.finditer(raw_text):
                word = m.group(1)
                if len(word) >= 2:
                    key = word.lower()
                    word_freq[key] += 1
                    if key not in word_source:
                        word_source[key] = "abbreviation"
                        word_confidence[key] = 0.7

        # Фильтрация и формирование предложений
        for key, freq in word_freq.items():
            if freq < min_frequency:
                continue
            if key in existing_lower:
                continue
            if _is_stop_word(key):
                continue
            if len(key) < self.min_word_length:
                continue

            conf = min(1.0, word_confidence.get(key, 0.7) + freq * 0.02)
            suggestions[key] = {
                "word": key,
                "frequency": freq,
                "source": word_source.get(key, "unknown"),
                "confidence": round(conf, 3),
            }

        # Сортируем по frequency desc, confidence desc
        result = sorted(
            suggestions.values(),
            key=lambda x: (-x["frequency"], -x["confidence"], x["word"]),
        )
        return result[:top_k]

    # ── Приватные методы ───────────────────────────────────────────────────────

    def _extract_misrecognized_words(
        self,
        items: List[Dict[str, Any]],
        min_frequency: int = 3,
    ) -> List[str]:
        """Извлекает слова из записей с низким confidence.

        Предположение: если слово часто встречается в «ненадёжных» транскрипциях
        (confidence < threshold), это может быть доменный термин, который Whisper
        плохо распознаёт — стоит добавить в vocab для подсказки.
        """
        word_freq: Counter = Counter()

        for item in items:
            conf = float(item.get("confidence", 1.0) or 1.0)
            if conf >= _LOW_CONFIDENCE_THRESHOLD:
                continue  # пропускаем уверенные записи

            raw = str(item.get("source_text", "") or item.get("text", "") or "")
            if not raw.strip():
                continue

            words = _RE_WORD.findall(raw)
            for w in words:
                if len(w) >= self.min_word_length and not _is_stop_word(w):
                    word_freq[w.lower()] += 1

        return [w for w, cnt in word_freq.items() if cnt >= min_frequency]

    def _extract_domain_terms(
        self,
        items: List[Dict[str, Any]],
        min_frequency: int = 3,
        recent_n: int = 50,
    ) -> List[str]:
        """Извлекает доменно-специфические термины из последних контекстов.

        Берёт последние `recent_n` записей, подсчитывает частоту слов,
        исключает стоп-слова — возвращает слова встречающиеся >= min_frequency раз.
        """
        recent = items[:recent_n]
        word_freq: Counter = Counter()

        for item in recent:
            raw = str(item.get("source_text", "") or item.get("text", "") or "")
            if not raw.strip():
                continue
            words = _RE_WORD.findall(raw)
            for w in words:
                if len(w) >= self.min_word_length and not _is_stop_word(w):
                    word_freq[w] += 1

        # Оставляем только слова без заглавной буквы (proper nouns обрабатываются отдельно)
        # и не-технические (без цифр / CamelCase)
        result = []
        for w, cnt in word_freq.items():
            if cnt < min_frequency:
                continue
            # Пропускаем чисто-заглавные (аббревиатуры/proper nouns уже покрыты)
            if w.isupper():
                continue
            # Пропускаем слова с цифрами (покрыты technical_terms)
            if any(c.isdigit() for c in w):
                continue
            result.append(w)

        return result
