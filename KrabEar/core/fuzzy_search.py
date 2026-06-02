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

        # Защита от memory-bomb: обрабатываем не более 5000 последних текстов
        MAX_TEXTS = 5000
        processed = 0

        for idx in range(len(texts) - 1, -1, -1):
            if processed >= MAX_TEXTS:
                break

            text = texts[idx]
            if not text:
                continue

            # Оптимизация: пропускаем слишком короткие тексты
            if len(text) < min_text_len:
                continue

            processed += 1
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
        if query == text:
            return 1.0
        if query in text:
            return 1.0

        # Защита от ReDoS O(N*M)
        query = query[:2000]
        text = text[:2000]

        # Полное совпадение всего текста с запросом
        full_score = difflib.SequenceMatcher(None, query, text).ratio()
        if full_score >= 0.99:
            return full_score

        # Частичный матчинг: умное скользящее окно
        partial_score = self._partial_ratio(query, text)

        return max(full_score, partial_score)

    def _partial_ratio(self, query: str, text: str) -> float:
        """Лучшее совпадение query с подстрокой text, используя якоря."""
        q_len = len(query)
        t_len = len(text)

        if q_len == 0 or t_len == 0:
            return 0.0

        if q_len >= t_len:
            return difflib.SequenceMatcher(None, query, text).ratio()

        best = 0.0
        # Ищем общие блоки, чтобы использовать их как якоря для окон
        blocks = difflib.SequenceMatcher(None, query, text).get_matching_blocks()

        checked_starts = set()

        for block in blocks:
            if block.size == 0:
                continue

            # Ожидаемое начало окна в text, чтобы block.a и block.b совпали
            expected_start = block.b - block.a

            # Проверяем само окно и небольшие смещения (для вставок/удалений до совпадения)
            for offset in (-2, -1, 0, 1, 2):
                start = expected_start + offset
                start = max(0, min(start, t_len - q_len))

                if start in checked_starts:
                    continue
                checked_starts.add(start)

                window = text[start:start + q_len]
                ratio = difflib.SequenceMatcher(None, query, window).ratio()
                if ratio > best:
                    best = ratio
                if best >= 0.99:
                    return best

        return best
