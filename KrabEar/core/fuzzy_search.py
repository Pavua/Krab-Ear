"""FuzzySearcher — нечёткий поиск по текстам без внешних зависимостей.

Использует difflib.SequenceMatcher для вычисления степени сходства.
Поддерживает подстрочный (partial) матчинг: запрос может быть частью текста.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass
class FuzzyMatch:
    """Результат нечёткого поиска."""
    index: int        # индекс текста в исходном списке
    score: float      # степень сходства [0.0, 1.0]
    matched_text: str  # текст, в котором найдено совпадение


class FuzzySearcher:
    """Нечёткий поиск по списку текстов с помощью difflib.SequenceMatcher."""

    def search(
        self,
        query: str,
        texts: list[str],
        threshold: float = 0.6,
    ) -> list[FuzzyMatch]:
        """Найти тексты, похожие на query.

        Args:
            query: поисковый запрос.
            texts: список текстов для поиска.
            threshold: минимальный порог сходства [0.0, 1.0].

        Returns:
            Список FuzzyMatch, отсортированный по убыванию score.
        """
        if not query:
            return []

        query_lower = query.lower()
        query_len = len(query_lower)
        min_text_len = max(1, query_len // 3)

        results: list[FuzzyMatch] = []

        for idx, text in enumerate(texts):
            if not text:
                continue
            # Оптимизация: пропускаем слишком короткие тексты
            if len(text) < min_text_len:
                continue

            score = self._score(query_lower, text.lower())
            if score >= threshold:
                results.append(FuzzyMatch(index=idx, score=score, matched_text=text))

        results.sort(key=lambda m: m.score, reverse=True)
        return results

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _score(self, query: str, text: str) -> float:
        """Вычислить максимальный score между полным и частичным совпадением."""
        # Полное совпадение всего текста с запросом
        full_score = difflib.SequenceMatcher(None, query, text).ratio()

        # Частичный матчинг: скользящее окно по тексту размером len(query)
        partial_score = self._partial_ratio(query, text)

        return max(full_score, partial_score)

    def _partial_ratio(self, query: str, text: str) -> float:
        """Лучшее совпадение query с подстрокой text длиной ~len(query)."""
        q_len = len(query)
        t_len = len(text)

        if q_len == 0 or t_len == 0:
            return 0.0

        # Если запрос длиннее текста — сравниваем целиком
        if q_len >= t_len:
            return difflib.SequenceMatcher(None, query, text).ratio()

        best = 0.0
        # Скользящее окно: шаг 1, но для длинных текстов можно шагать крупнее
        step = max(1, (t_len - q_len) // 20)
        positions = list(range(0, t_len - q_len + 1, step))
        # Всегда включаем последнюю позицию
        if (t_len - q_len) not in positions:
            positions.append(t_len - q_len)

        for i in positions:
            window = text[i: i + q_len]
            ratio = difflib.SequenceMatcher(None, query, window).ratio()
            if ratio > best:
                best = ratio
            if best == 1.0:
                break

        return best
