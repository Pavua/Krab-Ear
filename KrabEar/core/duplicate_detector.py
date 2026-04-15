"""DuplicateDetector — обнаружение дублирующихся транскрипций в истории Krab Ear.

Использует SequenceMatcher для вычисления текстового сходства.
Оптимизация: сравниваются только записи в пределах 60-секундного временного окна.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import List


@dataclass
class DuplicateGroup:
    """Группа похожих записей истории."""

    items: List[dict] = field(default_factory=list)
    similarity: float = 0.0


class DuplicateDetector:
    """Обнаруживает дублирующиеся транскрипции по текстовому сходству."""

    DEFAULT_TIME_WINDOW_SECONDS: int = 60

    @staticmethod
    def is_duplicate(text1: str, text2: str, threshold: float = 0.9) -> bool:
        """Возвращает True, если тексты похожи выше порогового значения."""
        if not text1 or not text2:
            return False
        ratio = SequenceMatcher(None, text1.strip(), text2.strip()).ratio()
        return ratio >= threshold

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

        Args:
            items: список записей истории (dict с полями text/ts).
            similarity_threshold: порог сходства [0..1], по умолчанию 0.9.

        Returns:
            Список DuplicateGroup — каждый элемент содержит 2+ похожих записи
            и максимальный коэффициент сходства внутри группы.
        """
        if not items:
            return []

        # Индекс уже назначенных в группу элементов
        assigned: set[int] = set()
        groups: list[DuplicateGroup] = []

        for i, item_i in enumerate(items):
            if i in assigned:
                continue

            text_i = self._get_text(item_i)
            if not text_i:
                continue

            ts_i = self._get_timestamp(item_i)
            group_indices: list[int] = [i]
            group_max_sim: float = 0.0

            for j, item_j in enumerate(items):
                if j <= i or j in assigned:
                    continue

                text_j = self._get_text(item_j)
                if not text_j:
                    continue

                # Проверка временного окна
                ts_j = self._get_timestamp(item_j)
                if ts_i is not None and ts_j is not None:
                    if abs(ts_i - ts_j) > self.DEFAULT_TIME_WINDOW_SECONDS:
                        continue

                ratio = SequenceMatcher(None, text_i, text_j).ratio()
                if ratio >= similarity_threshold:
                    group_indices.append(j)
                    if ratio > group_max_sim:
                        group_max_sim = ratio

            if len(group_indices) > 1:
                for idx in group_indices:
                    assigned.add(idx)
                groups.append(
                    DuplicateGroup(
                        items=[items[idx] for idx in group_indices],
                        similarity=round(group_max_sim, 4),
                    )
                )

        return groups
