"""Сравнение нескольких записей транскрибации Krab Ear.

Позволяет сравнивать до 10 записей истории side-by-side:
- матрица попарного сходства текстов (TF-IDF cosine similarity)
- статистика длительности / уверенности / языков
- общие слова и уникальные слова по каждой записи
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

MAX_ITEMS = 10

# Минимальная длина слова для включения в анализ (фильтрует предлоги и т.п.)
_MIN_WORD_LEN = 3

# Стоп-слова (RU + EN, базовый набор)
_STOP_WORDS: frozenset[str] = frozenset({
    # EN
    "the", "and", "for", "that", "this", "with", "are", "was", "were",
    "have", "has", "had", "not", "but", "from", "they", "you", "all",
    "can", "will", "just", "been", "more", "also", "than", "its",
    # RU
    "это", "как", "что", "для", "или", "его", "так", "уже", "она",
    "они", "мы", "вы", "им", "нет", "от", "до", "при", "по", "из",
    "на", "не", "со", "же", "бы", "то", "он", "то",
})


@dataclass
class ComparisonView:
    """Результат сравнения нескольких записей."""

    items: list[dict]
    """Полные данные каждой записи (в порядке запроса)."""

    text_similarity_matrix: list[list[float]]
    """NxN матрица попарного косинусного сходства (0.0–1.0)."""

    duration_comparison: dict
    """Статистика по полю audio_duration_sec: min / max / avg / std."""

    confidence_comparison: dict
    """Статистика по полю confidence: min / max / avg / std."""

    language_distribution: dict
    """Словарь {lang_code: count} — языки по всем записям."""

    common_words: list[str]
    """Слова, встречающиеся в КАЖДОЙ из записей."""

    unique_words_per_item: list[list[str]]
    """Список по каждому item: слова, встречающиеся ТОЛЬКО в этой записи."""


def _tokenize(text: str) -> set[str]:
    """Возвращает множество нормализованных токенов из текста."""
    if not text:
        return set()
    tokens = re.findall(r"[a-zA-Zа-яёА-ЯЁ]+", text.lower())
    return {t for t in tokens if len(t) >= _MIN_WORD_LEN and t not in _STOP_WORDS}


def _build_tf(tokens_list: list[set[str]]) -> list[dict[str, float]]:
    """Строит нормализованные TF-векторы (term frequency)."""
    result: list[dict[str, float]] = []
    for tokens in tokens_list:
        n = len(tokens)
        if n == 0:
            result.append({})
        else:
            tf = {t: 1.0 / n for t in tokens}  # binary TF / n
            result.append(tf)
    return result


def _cosine_sim(a: dict[str, float], b: dict[str, float]) -> float:
    """Косинусное сходство двух TF-словарей."""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[t] * b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return round(dot / (norm_a * norm_b), 4)


def _stat_dict(values: list[float]) -> dict:
    """Вычисляет min/max/avg/std для списка чисел (пустой → None везде)."""
    if not values:
        return {"min": None, "max": None, "avg": None, "std": None, "count": 0}
    n = len(values)
    mn = min(values)
    mx = max(values)
    avg = sum(values) / n
    variance = sum((v - avg) ** 2 for v in values) / n
    std = round(math.sqrt(variance), 4)
    return {
        "min": round(mn, 4),
        "max": round(mx, 4),
        "avg": round(avg, 4),
        "std": std,
        "count": n,
    }


class RecordingComparison:
    """Сервис сравнения нескольких записей транскрибации."""

    def compare(self, item_ids: list[str], store: Any) -> ComparisonView:
        """Сравнивает несколько записей и возвращает ComparisonView.

        Args:
            item_ids: список ID записей (от 2 до MAX_ITEMS).
            store: StateStore-совместимый объект с методом get_history_item_by_id.

        Returns:
            ComparisonView с матрицей сходства, статистикой и анализом слов.

        Raises:
            ValueError: если item_ids пустой, слишком длинный или содержит
                        несуществующие / дублирующиеся ID.
        """
        if not item_ids:
            raise ValueError("item_ids не может быть пустым")
        if len(item_ids) > MAX_ITEMS:
            raise ValueError(
                f"Максимальное количество записей для сравнения: {MAX_ITEMS}, "
                f"передано: {len(item_ids)}"
            )
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("item_ids содержит дубликаты")

        # Загружаем записи
        raw_items: list[dict] = []
        for item_id in item_ids:
            item = store.get_history_item_by_id(item_id)
            if item is None:
                raise ValueError(f"Запись с id={item_id!r} не найдена")
            raw_items.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))

        # Тексты записей для анализа
        texts = [str(it.get("text") or "") for it in raw_items]
        token_sets = [_tokenize(t) for t in texts]
        tf_vectors = _build_tf(token_sets)

        # NxN матрица сходства
        n = len(raw_items)
        sim_matrix: list[list[float]] = []
        for i in range(n):
            row: list[float] = []
            for j in range(n):
                if i == j:
                    row.append(1.0)
                elif i > j:
                    row.append(sim_matrix[j][i])  # симметрично
                else:
                    row.append(_cosine_sim(tf_vectors[i], tf_vectors[j]))
            sim_matrix.append(row)

        # Статистика длительности
        durations = [
            float(it["audio_duration_sec"])
            for it in raw_items
            if it.get("audio_duration_sec") is not None
        ]
        duration_comparison = _stat_dict(durations)

        # Статистика уверенности
        confidences = [
            float(it["confidence"])
            for it in raw_items
            if it.get("confidence") is not None
        ]
        confidence_comparison = _stat_dict(confidences)

        # Распределение языков
        lang_dist: dict[str, int] = {}
        for it in raw_items:
            lang = str(it.get("source_lang") or "").strip()
            if lang:
                lang_dist[lang] = lang_dist.get(lang, 0) + 1
        language_distribution = lang_dist

        # Общие слова и уникальные слова по каждому элементу
        if n >= 2:
            common_words = sorted(
                set.intersection(*token_sets) if all(token_sets) else set()
            )
        else:
            # Для одного элемента нет смысла, но формально возвращаем его слова
            common_words = sorted(token_sets[0]) if token_sets else []

        unique_words_per_item: list[list[str]] = []
        for i, tokens in enumerate(token_sets):
            others = set.union(*(token_sets[j] for j in range(n) if j != i)) if n > 1 else set()
            unique_words_per_item.append(sorted(tokens - others))

        return ComparisonView(
            items=raw_items,
            text_similarity_matrix=sim_matrix,
            duration_comparison=duration_comparison,
            confidence_comparison=confidence_comparison,
            language_distribution=language_distribution,
            common_words=common_words,
            unique_words_per_item=unique_words_per_item,
        )


def _view_to_dict(view: ComparisonView) -> dict:
    """Сериализует ComparisonView в словарь для IPC-ответа."""
    return {
        "items": view.items,
        "text_similarity_matrix": view.text_similarity_matrix,
        "duration_comparison": view.duration_comparison,
        "confidence_comparison": view.confidence_comparison,
        "language_distribution": view.language_distribution,
        "common_words": view.common_words,
        "unique_words_per_item": view.unique_words_per_item,
    }
