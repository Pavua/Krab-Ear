"""StageCache — LRU-кэш результатов между стадиями pipeline.

Кэширует результаты STT (и других стадий) по SHA-256 хэшу входных данных.
Thread-safe, LRU-eviction (max 100 записей на стадию), TTL-based invalidation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

logger = logging.getLogger("KrabEar.Pipeline.Cache")

_DEFAULT_MAX_ENTRIES = 100
_DEFAULT_TTL_SEC = 300


class _CacheEntry:
    """Запись кэша: результат + метаданные."""

    __slots__ = ("result", "expires_at", "stage_name", "input_hash")

    def __init__(self, result: dict, ttl_sec: int, stage_name: str, input_hash: str) -> None:
        self.result = result
        self.expires_at = time.monotonic() + ttl_sec
        self.stage_name = stage_name
        self.input_hash = input_hash

    def is_alive(self) -> bool:
        return time.monotonic() < self.expires_at


class StageCache:
    """LRU-кэш результатов стадий pipeline.

    Хранит результаты каждой стадии отдельно. Для каждой стадии поддерживается
    LRU-очередь (max_entries). Записи автоматически инвалидируются по TTL.

    Методы:
        compute_hash(data) -> str        — SHA-256 хэш для ключа кэша
        get(stage, hash) -> dict | None  — вернуть результат или None (miss)
        put(stage, hash, result, ttl)    — сохранить результат
        invalidate(stage=None)           — очистить одну или все стадии
        get_stats() -> dict              — hits, misses, hit_rate, entries per stage
    """

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        # { stage_name: OrderedDict[input_hash, _CacheEntry] }
        self._stores: Dict[str, OrderedDict] = {}
        self._lock = threading.Lock()

        # Статистика
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    @staticmethod
    def compute_hash(data: Any) -> str:
        """Вычислить SHA-256 хэш данных.

        Принимает:
        - bytes / bytearray: хэширует напрямую
        - numpy ndarray: хэширует raw bytes
        - str: кодирует в UTF-8, затем хэширует
        - dict / list: сериализует в JSON (sorted keys), затем хэширует
        - остальное: repr() → UTF-8 → хэш
        """
        try:
            import numpy as np  # type: ignore
            if isinstance(data, np.ndarray):
                raw = data.tobytes()
                return hashlib.sha256(raw).hexdigest()
        except ImportError:
            pass

        if isinstance(data, (bytes, bytearray)):
            return hashlib.sha256(data).hexdigest()

        if isinstance(data, str):
            return hashlib.sha256(data.encode("utf-8")).hexdigest()

        if isinstance(data, (dict, list)):
            try:
                serialized = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
                return hashlib.sha256(serialized).hexdigest()
            except (TypeError, ValueError):
                pass

        # Fallback: repr
        return hashlib.sha256(repr(data).encode("utf-8")).hexdigest()

    def get(self, stage_name: str, input_hash: str) -> Optional[dict]:
        """Вернуть кэшированный результат или None при cache miss.

        Если запись найдена, но истёк TTL — удаляет её и возвращает None.
        """
        with self._lock:
            store = self._stores.get(stage_name)
            if store is None:
                self._misses += 1
                return None

            entry = store.get(input_hash)
            if entry is None:
                self._misses += 1
                return None

            if not entry.is_alive():
                # Запись протухла — удаляем
                del store[input_hash]
                self._misses += 1
                logger.debug("Cache miss (expired): stage=%s hash=%s", stage_name, input_hash[:8])
                return None

            # LRU: перемещаем в конец (recently used)
            store.move_to_end(input_hash)
            self._hits += 1
            logger.debug("Cache hit: stage=%s hash=%s", stage_name, input_hash[:8])
            return entry.result

    def put(
        self,
        stage_name: str,
        input_hash: str,
        result: dict,
        ttl_sec: int = _DEFAULT_TTL_SEC,
    ) -> None:
        """Сохранить результат стадии в кэш.

        Если достигнут лимит max_entries — вытесняет LRU-запись (oldest).
        """
        with self._lock:
            if stage_name not in self._stores:
                self._stores[stage_name] = OrderedDict()

            store = self._stores[stage_name]

            # Если ключ уже есть — удаляем, чтобы обновить позицию в LRU
            if input_hash in store:
                del store[input_hash]

            # LRU eviction если достигнут лимит
            while len(store) >= self._max_entries:
                evicted_hash, evicted = store.popitem(last=False)
                logger.debug(
                    "Cache evict (LRU): stage=%s hash=%s", stage_name, evicted_hash[:8]
                )

            entry = _CacheEntry(
                result=result,
                ttl_sec=ttl_sec,
                stage_name=stage_name,
                input_hash=input_hash,
            )
            store[input_hash] = entry
            logger.debug(
                "Cache put: stage=%s hash=%s ttl=%ds entries=%d",
                stage_name,
                input_hash[:8],
                ttl_sec,
                len(store),
            )

    def invalidate(self, stage_name: Optional[str] = None) -> None:
        """Инвалидировать кэш.

        Args:
            stage_name: если указан — очищает только эту стадию.
                        если None — очищает все стадии.
        """
        with self._lock:
            if stage_name is None:
                cleared = sum(len(s) for s in self._stores.values())
                self._stores.clear()
                logger.debug("Cache invalidate ALL: %d entries removed", cleared)
            else:
                store = self._stores.pop(stage_name, None)
                cleared = len(store) if store else 0
                logger.debug(
                    "Cache invalidate stage=%s: %d entries removed", stage_name, cleared
                )

    def get_stats(self) -> dict:
        """Вернуть статистику кэша.

        Returns:
            dict с полями:
            - hits: int
            - misses: int
            - hit_rate: float (0.0–1.0)
            - total_entries: int
            - entries_per_stage: dict[stage_name, int]
        """
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0

            entries_per_stage = {
                stage: len(store) for stage, store in self._stores.items()
            }
            total_entries = sum(entries_per_stage.values())

            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
                "total_entries": total_entries,
                "entries_per_stage": entries_per_stage,
            }

    def reset_stats(self) -> None:
        """Сбросить счётчики hits/misses (не очищает сами записи)."""
        with self._lock:
            self._hits = 0
            self._misses = 0
