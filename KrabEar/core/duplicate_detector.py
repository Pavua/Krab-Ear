"""DuplicateDetector — обнаружение дублирующихся транскрипций в истории Krab Ear.

Использует SequenceMatcher для вычисления текстового сходства с защитой от
ReDoS (входные строки ограничены 2000 символами, O(N^2) сложность безопасна).
Оптимизация: сравниваются только записи в пределах 60-секундного временного окна.
Группировка: центроидная кластеризация для предотвращения транзитивного дрейфа
(false-merge когда A≈B и B≈C ошибочно приводит к A≈C).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class DuplicateGroup:
    """Группа похожих записей истории."""

    items: List[dict] = field(default_factory=list)
    similarity: float = 0.0


class DuplicateDetector:
    """Обнаруживает дублирующиеся транскрипции по текстовому сходству."""

    DEFAULT_TIME_WINDOW_SECONDS: int = 60

    @staticmethod
    def calculate_similarity(text1: str, text2: str) -> float:
        """Вычисляет сходство двух текстов (SequenceMatcher), защищённое от ReDoS.

        Входные строки обрезаются до 2000 символов перед сравнением,
        что гарантирует O(N^2) = O(4_000_000) в худшем случае.
        """
        if not text1 or not text2:
            return 0.0

        # ReDoS guard: SequenceMatcher is O(N^2) — cap at 2000 chars
        text1 = text1.strip()[:2000]
        text2 = text2.strip()[:2000]

        if not text1 or not text2:
            return 0.0

        from difflib import SequenceMatcher
        return SequenceMatcher(None, text1, text2).ratio()

    @staticmethod
    def is_duplicate(text1: str, text2: str, threshold: float = 0.9) -> bool:
        """Возвращает True, если тексты похожи выше порогового значения."""
        return DuplicateDetector.calculate_similarity(text1, text2) >= threshold

    @staticmethod
    def _get_timestamp(item: dict) -> float | None:
        """Извлекает Unix-timestamp из записи истории."""
        ts = item.get("ts") or item.get("timestamp") or item.get("created_at")
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return float(ts)
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _get_text(item: dict) -> str:
        """Извлекает основной текст записи истории."""
        return str(item.get("text") or item.get("transcript") or "").strip()

    def find_duplicates(
        self,
        items: list[dict],
        similarity_threshold: float = 0.9,
    ) -> list[DuplicateGroup]:
        """Находит группы похожих записей в пределах 60-секундного окна.

        Использует центроидную кластеризацию (greedy): каждый кандидат
        сравнивается строго с лидером группы (первым элементом), что
        предотвращает транзитивный дрейф (A≈B и B≈C не ведут к A≈C).

        Args:
            items: список записей истории (dict с полями text/ts).
            similarity_threshold: порог сходства [0..1], по умолчанию 0.9.

        Returns:
            Список DuplicateGroup — каждый элемент содержит 2+ похожих записи
            и максимальный коэффициент сходства внутри группы.
        """
        if not items:
            return []

        n = len(items)
        texts: list[str] = [self._get_text(item) for item in items]
        timestamps: list[Optional[float]] = [self._get_timestamp(item) for item in items]

        groups: list[DuplicateGroup] = []
        used: set[int] = set()

        for i in range(n):
            if i in used or not texts[i]:
                continue

            current_group = [i]
            used.add(i)
            max_sim = 0.0

            for j in range(i + 1, n):
                if j in used or not texts[j]:
                    continue

                ts_i, ts_j = timestamps[i], timestamps[j]
                if ts_i is not None and ts_j is not None:
                    if abs(ts_i - ts_j) > self.DEFAULT_TIME_WINDOW_SECONDS:
                        continue

                # Compare strictly with group leader (centroid), not most-recent member
                ratio = self.calculate_similarity(texts[i], texts[j])
                if ratio >= similarity_threshold:
                    current_group.append(j)
                    used.add(j)
                    if ratio > max_sim:
                        max_sim = ratio

            if len(current_group) >= 2:
                groups.append(
                    DuplicateGroup(
                        items=[items[idx] for idx in current_group],
                        similarity=round(max_sim, 4),
                    )
                )

        return groups
