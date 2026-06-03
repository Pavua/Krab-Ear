"""LanguageLearningManager — режим изучения языков для Krab Ear.

Извлекает словарь из двуязычных транскрипций, формирует флеш-карточки
и предоставляет статистику прогресса изучения языка.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import logging

# C3 DoS guard: максимальное число записей истории, которое анализируют
# get_learning_stats / extract_vocabulary за один IPC-вызов.
# Без лимита злоумышленник мог подать ≥100k записей → O(N) tokenize+Counter → зависание.
_MAX_ITEMS_FOR_STATS = 1000

logger = logging.getLogger("KrabEar.Backend.LanguageLearning")

# ---------------------------------------------------------------------------
# Стоп-слова (RU + ES + EN) — фильтруем функциональные слова из словаря
# ---------------------------------------------------------------------------
_STOP_WORDS: frozenset = frozenset({
    # RU
    "в", "на", "с", "по", "из", "от", "до", "за", "под", "над", "к", "о",
    "об", "про", "при", "для", "без", "через", "между", "перед", "после",
    "во", "со", "ко", "не", "ни", "бы", "же", "ли", "и", "а", "но", "да",
    "то", "или", "что", "как", "так", "уже", "ещё", "еще", "все", "этот",
    "это", "эта", "этой", "этого", "этим", "этих", "он", "она", "оно", "они",
    "мы", "вы", "я", "его", "её", "ее", "их", "мой", "твой", "наш", "ваш",
    "свой", "себя", "тот", "та", "те", "такой", "такие", "быть", "есть",
    "был", "была", "были", "будет", "будут", "там", "здесь", "тут", "где",
    "когда", "потому", "потом", "затем", "вот", "ну", "вдруг", "если", "нет",
    "очень", "более", "менее", "больше", "меньше", "можно", "нужно", "надо",
    # ES
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "al", "en", "con", "por", "para", "sin", "sobre", "entre", "ante",
    "bajo", "desde", "hasta", "hacia", "durante", "y", "e", "o", "u",
    "pero", "sino", "que", "como", "si", "se", "me", "te", "le", "nos",
    "os", "les", "lo", "su", "sus", "mi", "mis", "tu", "tus", "este",
    "esta", "estos", "estas", "ese", "esa", "esos", "esas", "yo",
    "él", "ella", "ellos", "ellas", "usted", "ustedes", "nosotros",
    "vosotros", "es", "son", "era", "fue", "ser", "estar", "hay", "ya",
    "no", "más", "muy", "bien", "también", "sí", "así", "todo", "todos",
    # EN
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "by",
    "from", "up", "about", "into", "through", "during", "before", "after",
    "above", "below", "between", "out", "off", "over", "under", "again",
    "and", "but", "or", "nor", "so", "yet", "both", "either", "neither",
    "not", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "can", "must", "it", "its", "this", "that",
    "these", "those", "i", "you", "he", "she", "we", "they", "me", "him",
    "her", "us", "them", "my", "your", "his", "our", "their", "what",
    "which", "who", "when", "where", "how", "all", "each", "more", "also",
})


def _tokenize(text: str) -> list[str]:
    """Разбивает текст на слова (нижний регистр, только буквы)."""
    return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)


def _difficulty_from_frequency(frequency: int, max_freq: int) -> str:
    """Определяет сложность слова на основе частотности.

    Редкие слова сложнее (frequency < 33% max → hard).
    Средние (33–66% max → medium).
    Частые (> 66% max → easy).
    """
    if max_freq <= 0:
        return "medium"
    ratio = frequency / max_freq
    if ratio >= 0.66:
        return "easy"
    if ratio >= 0.33:
        return "medium"
    return "hard"


# ---------------------------------------------------------------------------
# VocabEntry — единица словаря
# ---------------------------------------------------------------------------

@dataclass
class VocabEntry:
    """Запись словаря: слово в исходном языке, перевод и контекст."""

    word_source: str
    """Слово на исходном языке."""

    word_target: str
    """Слово/перевод на целевом языке."""

    context_sentence: str
    """Предложение-пример из реальной транскрипции."""

    frequency: int
    """Сколько раз слово встречалось в текстах."""

    first_seen: str
    """ISO-timestamp первого появления слова (из метаданных записи)."""


# ---------------------------------------------------------------------------
# LanguageLearningManager
# ---------------------------------------------------------------------------

class LanguageLearningManager:
    """Анализирует двуязычные транскрипции и помогает изучать язык.

    Работает непосредственно с данными истории StateStore или принимает
    готовые списки объектов HistoryItem (или dict-совместимых объектов).
    """

    def extract_vocabulary(
        self,
        items: list,
        source_lang: str,
        target_lang: str,
    ) -> list[VocabEntry]:
        """Извлекает словарь из двуязычных транскрипций.

        Перебирает записи истории, у которых есть оригинальный текст
        (``source_text`` или ``text``) и перевод (``translated_text``),
        токенизирует исходный текст и строит список VocabEntry.

        Args:
            items: список HistoryItem или dict-совместимых объектов.
            source_lang: язык исходного текста (например "ru", "es").
            target_lang: язык перевода (например "es", "en").

        Returns:
            Список VocabEntry, отсортированный по убыванию частотности.
        """
        # Счётчики частотности
        word_freq: Counter = Counter()
        # Хранит для каждого слова: (context_sentence, first_seen, translation_snippet)
        word_meta: dict[str, tuple[str, str, str]] = {}

        for item in items:
            src = self._get_field(item, "source_text") or self._get_field(item, "text") or ""
            tgt = self._get_field(item, "translated_text") or ""
            item_src_lang = self._get_field(item, "source_lang") or ""
            item_tgt_lang = self._get_field(item, "target_lang") or ""
            ts = self._get_field(item, "ts") or datetime.now().isoformat(timespec="seconds")

            # Принимаем записи с совпадением языков (или если lang не указан)
            src_match = (not item_src_lang) or (item_src_lang.lower() == source_lang.lower())
            tgt_match = (not item_tgt_lang) or (item_tgt_lang.lower() == target_lang.lower())

            if not (src_match and tgt_match):
                continue
            if not src.strip():
                continue

            tokens = [
                w for w in _tokenize(src)
                if w not in _STOP_WORDS and len(w) >= 3
            ]

            # Перевод: берём первое слово из tgt как «перевод» для контекста
            tgt_tokens = [
                w for w in _tokenize(tgt)
                if w not in _STOP_WORDS and len(w) >= 3
            ] if tgt.strip() else []

            for i, word in enumerate(tokens):
                word_freq[word] += 1
                if word not in word_meta:
                    # Контекст: предложение-источник (до 150 символов)
                    context = src.strip()
                    if len(context) > 150:
                        context = context[:150] + "…"
                    # Перевод для слова: берём слово из tgt по тому же индексу если есть,
                    # иначе — первое слово перевода, иначе пустую строку
                    if tgt_tokens:
                        word_tgt = tgt_tokens[i] if i < len(tgt_tokens) else tgt_tokens[0]
                    else:
                        word_tgt = ""
                    word_meta[word] = (context, ts, word_tgt)

        if not word_freq:
            return []

        result: list[VocabEntry] = []
        for word, freq in word_freq.most_common():
            context, first_seen, word_tgt = word_meta[word]
            result.append(VocabEntry(
                word_source=word,
                word_target=word_tgt,
                context_sentence=context,
                frequency=freq,
                first_seen=first_seen,
            ))

        return result

    def generate_flashcards(
        self,
        items: list,
        source_lang: str,
        target_lang: str,
        max_cards: int = 20,
    ) -> list[dict]:
        """Генерирует флеш-карточки для изучения языка.

        Карточки строятся на основе словаря из двуязычных транскрипций.
        Сложность определяется по частотности слова.

        Args:
            items: список HistoryItem или dict-совместимых объектов.
            source_lang: язык исходного текста.
            target_lang: язык перевода.
            max_cards: максимальное количество карточек (по умолчанию 20).

        Returns:
            Список dict с ключами: front, back, context, difficulty.
        """
        vocab = self.extract_vocabulary(items, source_lang, target_lang)
        if not vocab:
            return []

        max_freq = vocab[0].frequency if vocab else 1

        # Берём разнообразные карточки: сначала средней сложности, потом hard, easy
        sorted_vocab = sorted(
            vocab,
            key=lambda e: (
                0 if _difficulty_from_frequency(e.frequency, max_freq) == "medium"
                else 1 if _difficulty_from_frequency(e.frequency, max_freq) == "hard"
                else 2
            ),
        )

        cards: list[dict] = []
        for entry in sorted_vocab[:max_cards]:
            difficulty = _difficulty_from_frequency(entry.frequency, max_freq)
            cards.append({
                "front": entry.word_source,
                "back": entry.word_target or f"[{target_lang}]",
                "context": entry.context_sentence,
                "difficulty": difficulty,
            })

        return cards

    def get_learning_stats(
        self,
        items: list,
        source_lang: str,
        target_lang: str,
    ) -> dict:
        """Возвращает статистику прогресса изучения языка.

        Args:
            items: список HistoryItem или dict-совместимых объектов.
            source_lang: язык исходного текста.
            target_lang: язык перевода.

        Returns:
            dict с полями:
                unique_words — количество уникальных слов в словаре,
                total_occurrences — суммарное число вхождений,
                frequency_distribution — распределение {easy, medium, hard},
                top_words — топ-10 самых частотных слов,
                source_lang — исходный язык,
                target_lang — целевой язык,
                items_scanned — сколько записей фактически обработано (≤ _MAX_ITEMS_FOR_STATS).
        """
        # C3 DoS guard: берём последние _MAX_ITEMS_FOR_STATS записей.
        # Свежие записи приоритетнее для словарной статистики.
        items_capped = items[-_MAX_ITEMS_FOR_STATS:] if len(items) > _MAX_ITEMS_FOR_STATS else items
        vocab = self.extract_vocabulary(items_capped, source_lang, target_lang)

        unique_words = len(vocab)
        total_occurrences = sum(e.frequency for e in vocab)

        max_freq = vocab[0].frequency if vocab else 1
        distribution: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
        for entry in vocab:
            d = _difficulty_from_frequency(entry.frequency, max_freq)
            distribution[d] = distribution.get(d, 0) + 1

        top_words = [
            {"word": e.word_source, "translation": e.word_target, "frequency": e.frequency}
            for e in vocab[:10]
        ]

        return {
            "unique_words": unique_words,
            "total_occurrences": total_occurrences,
            "frequency_distribution": distribution,
            "top_words": top_words,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "items_scanned": len(items_capped),
        }

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_extract_learning_vocabulary(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: extract_learning_vocabulary.

        Параметры:
            source_lang (str): язык исходного текста.
            target_lang (str): язык перевода.
            store (StateStore, optional): StateStore для загрузки истории.
            items (list, optional): явный список записей (для тестов/переопределения).
            limit (int, optional): максимальное количество записей словаря. Default 100.
        """
        source_lang = str(params.get("source_lang", "")).strip()
        target_lang = str(params.get("target_lang", "")).strip()
        limit = int(params.get("limit", 100))

        if not source_lang:
            raise RuntimeError("Параметр 'source_lang' обязателен")
        if not target_lang:
            raise RuntimeError("Параметр 'target_lang' обязателен")

        items = self._resolve_items(params)
        vocab = self.extract_vocabulary(items, source_lang, target_lang)

        return {
            "vocabulary": [
                {
                    "word_source": e.word_source,
                    "word_target": e.word_target,
                    "context_sentence": e.context_sentence,
                    "frequency": e.frequency,
                    "first_seen": e.first_seen,
                }
                for e in vocab[:limit]
            ],
            "total": len(vocab),
            "source_lang": source_lang,
            "target_lang": target_lang,
        }

    def handle_generate_flashcards(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: generate_flashcards.

        Параметры:
            source_lang (str): язык исходного текста.
            target_lang (str): язык перевода.
            max_cards (int, optional): максимальное количество карточек. Default 20.
            store (StateStore, optional): StateStore для загрузки истории.
            items (list, optional): явный список записей.
        """
        source_lang = str(params.get("source_lang", "")).strip()
        target_lang = str(params.get("target_lang", "")).strip()
        max_cards = int(params.get("max_cards", 20))

        if not source_lang:
            raise RuntimeError("Параметр 'source_lang' обязателен")
        if not target_lang:
            raise RuntimeError("Параметр 'target_lang' обязателен")

        items = self._resolve_items(params)
        cards = self.generate_flashcards(items, source_lang, target_lang, max_cards=max_cards)

        return {
            "cards": cards,
            "total": len(cards),
            "source_lang": source_lang,
            "target_lang": target_lang,
        }

    def handle_get_learning_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_learning_stats.

        Параметры:
            source_lang (str): язык исходного текста.
            target_lang (str): язык перевода.
            store (StateStore, optional): StateStore для загрузки истории.
            items (list, optional): явный список записей.
        """
        source_lang = str(params.get("source_lang", "")).strip()
        target_lang = str(params.get("target_lang", "")).strip()

        if not source_lang:
            raise RuntimeError("Параметр 'source_lang' обязателен")
        if not target_lang:
            raise RuntimeError("Параметр 'target_lang' обязателен")

        items = self._resolve_items(params)
        stats = self.get_learning_stats(items, source_lang, target_lang)
        return stats

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _get_field(item: Any, field: str) -> str:
        """Получает поле из объекта (dataclass/dict)."""
        if isinstance(item, dict):
            return item.get(field, "") or ""
        return getattr(item, field, "") or ""

    @staticmethod
    def _resolve_items(params: dict[str, Any]) -> list:
        """Возвращает список записей истории из параметров или store."""
        # Явный список записей (для тестов/переопределения)
        if "items" in params and isinstance(params["items"], list):
            return params["items"]

        # Загрузка из StateStore
        store = params.get("store")
        if store is not None:
            try:
                with store._lock():
                    return store._load_active_items_unlocked()
            except Exception:
                logger.exception("Ошибка при загрузке истории из StateStore")
                return []

        return []
