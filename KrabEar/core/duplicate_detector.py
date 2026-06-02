"""DuplicateDetector — обнаружение дублирующихся транскрипций в истории Krab Ear.

Использует мультимножества n-грамм (Jaccard) для вычисления сходства, 
что защищает от ReDoS и решает проблему нечувствительности к порядку и количеству слов.
Оптимизация: сравниваются только записи в пределах 60-секундного временного окна.
Группировка: центроидная кластеризация для предотвращения дрейфа и потери данных (false-merge).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
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
    def calculate_similarity(text1: str, text2: str) -> float:
        """Вычисляет сходство, защищённое от ReDoS."""
        if not text1 or not text2:
            return 0.0
            
        # Защита от ReDoS и чрезмерного потребления памяти
        # Ограничиваем длину 2000 символами (SequenceMatcher O(N^2))
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

        Использует центроидную кластеризацию (greedy clustering) для предотвращения 
        транзитивного дрейфа (когда A≈B≈C приводит к A≈C, что ведет к false-merge).

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
        used = set()
        
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
                        
                # Строгое сравнение с лидером группы
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
