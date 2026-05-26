"""Персистентный LRU-кэш переводов для Krab Ear.

Хранит результаты перевода на диске (translation_cache.json) и в памяти
(OrderedDict, LRU-вытеснение). Ключ — хэш (text + source + target + engine).
Максимум 5000 записей; при превышении удаляются самые старые.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("KrabEar.Backend.TranslationCache")

_MAX_ENTRIES = 5000
_CACHE_FILENAME = "translation_cache.json"


def _make_key(text: str, source: str, target: str, engine: str) -> str:
    """Возвращает SHA-256 hex-хэш параметров перевода."""
    raw = f"{text}\x00{source}\x00{target}\x00{engine}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TranslationCache:
    """LRU-кэш переводов с персистентностью на диск.

    Потокобезопасен (threading.Lock).
    """

    def __init__(self, data_dir: str, max_entries: int = _MAX_ENTRIES) -> None:
        self._data_dir = data_dir
        self._max_entries = max_entries
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()
        self._path = os.path.join(data_dir, _CACHE_FILENAME)
        self._load()

    # ── Public API ──────────────────────────────────────────────────────

    def get(self, text: str, source: str, target: str, engine: str) -> Optional[str]:
        """Возвращает кэшированный перевод или None."""
        key = _make_key(text, source, target, engine)
        with self._lock:
            value = self._cache.get(key)
            if value is None:
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def put(self, text: str, source: str, target: str, engine: str, result: str) -> None:
        """Сохраняет результат перевода в кэш и персистирует на диск."""
        key = _make_key(text, source, target, engine)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = result
            self._cache.move_to_end(key)
            # Вытесняем старые записи
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
            # Персистируем под тем же lock — snapshot берём здесь, до release
            self._persist(snapshot=dict(self._cache))

    def get_stats(self) -> dict:
        """Возвращает статистику кэша."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
                "entries": len(self._cache),
            }

    def clear(self) -> None:
        """Очищает кэш (память + файл)."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            # Персистируем под тем же lock — snapshot пустой dict
            self._persist(snapshot={})

    # ── Private helpers ─────────────────────────────────────────────────

    def _load(self) -> None:
        """Загружает кэш с диска при инициализации."""
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                # Восстанавливаем порядок; обрезаем до лимита
                items = list(data.items())[-self._max_entries:]
                self._cache = OrderedDict(items)
        except Exception as exc:
            logger.warning("Не удалось загрузить translation_cache.json: %s", exc)

    def _persist(self, snapshot: Optional[dict] = None) -> None:
        """Записывает кэш на диск атомарно через .tmp + os.replace.

        Принимает необязательный ``snapshot`` — уже снятую копию кэша.
        Если не передан — берёт snapshot под self._lock самостоятельно
        (для обратной совместимости с возможными внешними вызовами).

        F2: fh.flush() + os.fsync() перед os.replace гарантируют, что данные
        попадают на диск до атомарного rename — защита от потери при сбое питания.
        F5: callers (put/clear) передают snapshot изнутри своего with self._lock,
        поэтому повторное взятие lock здесь не нужно.
        """
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            if snapshot is None:
                with self._lock:
                    snapshot = dict(self._cache)
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception as exc:
            logger.warning("Не удалось сохранить translation_cache.json: %s", exc)
