"""DuplicateDetector — обнаружение дублирующихся транскрипций в истории Krab Ear.

Использует SequenceMatcher для вычисления текстового сходства.
Оптимизация: сравниваются только записи в пределах 60-секундного временного окна.
Группировка: union-find для транзитивного объединения (A≈B, B≈C → {A,B,C}).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional


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

        Использует union-find для корректного транзитивного объединения:
        если A≈B и B≈C, то {A,B,C} попадают в одну группу, даже если A≉C.

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

        # Union-Find: инициализируем каждый элемент как свой собственный корень
        parent: list[int] = list(range(n))

        def _find(x: int) -> int:
            """Находит корень с path compression."""
            while parent[x] != x:
                parent[x] = parent[parent[x]]  # сжатие пути (halving)
                x = parent[x]
            return x

        def _union(a: int, b: int) -> None:
            """Объединяет два множества."""
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[ra] = rb

        # Кэшируем тексты и временные метки (избегаем повторных вызовов)
        texts: list[str] = [self._get_text(item) for item in items]
        timestamps: list[Optional[float]] = [self._get_timestamp(item) for item in items]

        # Попарное сравнение: объединяем схожие элементы
        # Отслеживаем максимальное сходство для каждой пары корней
        pair_max_sim: Dict[tuple, float] = {}

        for i in range(n):
            if not texts[i]:
                continue
            for j in range(i + 1, n):
                if not texts[j]:
                    continue

                # Проверка временного окна
                ts_i, ts_j = timestamps[i], timestamps[j]
                if ts_i is not None and ts_j is not None:
                    if abs(ts_i - ts_j) > self.DEFAULT_TIME_WINDOW_SECONDS:
                        continue

                ratio = SequenceMatcher(None, texts[i], texts[j]).ratio()
                if ratio >= similarity_threshold:
                    _union(i, j)
                    # Сохраняем максимальное сходство для последующего расчёта
                    ri, rj = _find(i), _find(j)
                    root_pair = (min(ri, rj), max(ri, rj))
                    if ratio > pair_max_sim.get(root_pair, 0.0):
                        pair_max_sim[root_pair] = ratio

        # Группируем элементы по корню union-find
        root_to_indices: Dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            if texts[i]:  # пропускаем элементы с пустым текстом
                root_to_indices[_find(i)].append(i)

        # Строим DuplicateGroup для групп из 2+ элементов
        groups: list[DuplicateGroup] = []
        for root, indices in root_to_indices.items():
            if len(indices) < 2:
                continue

            # Максимальное сходство внутри группы: пересчитываем точно
            group_items = [items[idx] for idx in indices]
            max_sim: float = 0.0
            group_texts = [texts[idx] for idx in indices]
            for gi in range(len(group_texts)):
                for gj in range(gi + 1, len(group_texts)):
                    r = SequenceMatcher(None, group_texts[gi], group_texts[gj]).ratio()
                    if r > max_sim:
                        max_sim = r

            groups.append(
                DuplicateGroup(
                    items=group_items,
                    similarity=round(max_sim, 4),
                )
            )

        return groups
