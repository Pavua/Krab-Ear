"""Инвертированный индекс для быстрого полнотекстового поиска по истории Krab Ear.

Поддерживает:
- мультисловные запросы (логика AND)
- нечувствительность к регистру
- базовый стемминг (удаление суффиксов RU)
- генерацию снипетов (30 символов вокруг совпадения)
- ленивую перестройку по хэш-сигнатуре
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Стемминг: список распространённых русских суффиксов (от длинных к коротким)
# ---------------------------------------------------------------------------

_RU_SUFFIXES = [
    # глагольные
    "ываться", "иваться", "оваться", "уваться",
    "ывать", "ивать", "овать", "увать",
    "ываю", "иваю", "ую", "юю",
    "ться", "тся", "ешь", "ишь",
    "ете", "ите", "ают", "яют", "ует", "юет",
    "ал", "ял", "ила", "али",
    # именные
    "ости", "ость", "ений", "ение", "ения",
    "ами", "ями", "ого", "его", "ому", "ему",
    "ой", "ей", "ую", "юю",
    "ах", "ях", "ов", "ев", "ий", "ый", "ая", "яя",
    "ие", "ые",
    "ам", "ям", "им", "ым",
    "ом", "ем", "ём",
    "у", "ю", "а", "я", "е", "и",
]

# минимальная длина корня после усечения суффикса
_MIN_STEM_LEN = 3


def _stem_ru(word: str) -> str:
    """Простое удаление суффиксов для русских слов."""
    if len(word) <= _MIN_STEM_LEN:
        return word
    for suffix in _RU_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM_LEN:
            return word[: len(word) - len(suffix)]
    return word


# Precompiled regex for tokenization — called on every history item and every query
_RE_TOKEN = re.compile(r"[а-яёa-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Разбивает текст на слова (unicode-aware), возвращает стемминизированные токены."""
    words = _RE_TOKEN.findall(text.lower())
    return [_stem_ru(w) for w in words]


# ---------------------------------------------------------------------------
# Dataclass результата
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    item_id: str
    score: int                          # число совпавших уникальных термов
    matched_terms: list[str] = field(default_factory=list)
    snippet: str = ""


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------


class SearchIndex:
    """Инвертированный индекс для быстрого полнотекстового поиска."""

    def __init__(self) -> None:
        # Защита от гонки между build_index и search в многопоточном окружении
        self._lock = threading.RLock()
        # {stemmed_token: set[item_id]}
        self._index: dict[str, set[str]] = {}
        # {item_id: raw_text} для генерации снипетов
        self._texts: dict[str, str] = {}
        # сигнатура для инвалидации
        self._signature: Optional[str] = None

    # ------------------------------------------------------------------
    # Построение
    # ------------------------------------------------------------------

    def build_index(self, items: list[dict]) -> None:
        """Строит инвертированный индекс из списка записей истории."""
        new_sig = self._compute_signature(items)
        with self._lock:
            if new_sig == self._signature:
                return  # данные не изменились

            new_index: dict[str, set[str]] = {}
            new_texts: dict[str, str] = {}

            for item in items:
                item_id = item.get("id")
                if not item_id:
                    continue
                raw = self._item_text(item)
                new_texts[item_id] = raw
                for token in _tokenize(raw):
                    if token not in new_index:
                        new_index[token] = set()
                    new_index[token].add(item_id)

            self._index = new_index
            self._texts = new_texts
            self._signature = new_sig

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 50) -> list[SearchResult]:
        """Возвращает список SearchResult, отсортированных по убыванию score."""
        query = query.strip()
        if not query:
            return []

        query_tokens = list(dict.fromkeys(_tokenize(query)))  # уникальные, порядок сохранён
        if not query_tokens:
            return []

        raw_words = _RE_TOKEN.findall(query.lower())

        with self._lock:
            # AND: пересечение множеств item_id для каждого токена
            candidate_sets: list[set[str]] = []
            matched_per_token: dict[str, str] = {}  # token -> original query word

            for i, token in enumerate(query_tokens):
                ids = self._index.get(token)
                if ids is None:
                    # Нет ни одного документа с этим токеном — AND не выполнимо
                    return []
                candidate_sets.append(ids)
                matched_per_token[token] = raw_words[i] if i < len(raw_words) else token

            common_ids: set[str] = candidate_sets[0].copy()
            for s in candidate_sets[1:]:
                common_ids &= s

            if not common_ids:
                return []

            results: list[SearchResult] = []
            for item_id in common_ids:
                raw = self._texts.get(item_id, "")
                snippet = self._make_snippet(raw, raw_words)
                results.append(
                    SearchResult(
                        item_id=item_id,
                        score=len(query_tokens),
                        matched_terms=list(matched_per_token.values()),
                        snippet=snippet,
                    )
                )

        # Сортируем по score (убывание), затем по item_id (детерминированность)
        results.sort(key=lambda r: (-r.score, r.item_id))
        return results[:limit]

    # ------------------------------------------------------------------
    # Статистика
    # ------------------------------------------------------------------

    def get_index_stats(self) -> dict:
        """Возвращает статистику индекса."""
        with self._lock:
            total_refs = sum(len(ids) for ids in self._index.values())
            return {
                "unique_words": len(self._index),
                "items_indexed": len(self._texts),
                "total_word_refs": total_refs,
                "signature": self._signature,
            }

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------

    @staticmethod
    def _item_text(item: dict) -> str:
        """Объединяет поля текста записи в одну строку."""
        parts = [
            item.get("text") or "",
            item.get("source_text") or "",
            item.get("translated_text") or "",
        ]
        return " ".join(parts)

    @staticmethod
    def _compute_signature(items: list[dict]) -> str:
        """Быстрая хэш-сигнатура для инвалидации кэша."""
        h = hashlib.md5()
        for item in items:
            item_id = item.get("id", "")
            text = (item.get("text") or "") + (item.get("translated_text") or "")
            h.update(f"{item_id}:{text}".encode("utf-8", errors="replace"))
        return h.hexdigest()

    @staticmethod
    def _make_snippet(text: str, raw_words: list[str], radius: int = 30) -> str:
        """Генерирует снипет: 30 символов вокруг первого совпадения."""
        text_lower = text.lower()
        for word in raw_words:
            pos = text_lower.find(word)
            if pos != -1:
                start = max(0, pos - radius)
                end = min(len(text), pos + len(word) + radius)
                prefix = "…" if start > 0 else ""
                suffix = "…" if end < len(text) else ""
                return prefix + text[start:end] + suffix
        return text[:60]
