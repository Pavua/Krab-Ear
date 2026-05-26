"""Персистентный LRU-кэш переводов для Krab Ear.

Хранит результаты перевода на диске (translation_cache.json) и в памяти
(OrderedDict, LRU-вытеснение). Ключ — хэш (text + source + target + engine +
network_mode). network_mode включён в ключ чтобы предотвратить возврат
online-результата для offline_strict-запроса (W1313 F1 HIGH — privacy bypass).
Максимум 5000 записей; при превышении удаляются самые старые.

Backward compat: старые ключи без network_mode имеют другой хэш → автоматически
трактуются как cache miss (нет специальной обработки).
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


def _make_key(text: str, source: str, target: str, engine: str, network_mode: str = "") -> str:
    """Возвращает SHA-256 hex-хэш параметров перевода.

    network_mode включён в ключ (W1313 F1 HIGH) чтобы предотвратить возврат
    online-кэшированного результата для offline_strict-запроса.
    Старые ключи без network_mode (пустая строка по умолчанию) имеют другой хэш
    от ключей с явным network_mode → автоматически трактуются как cache miss.
    """
    raw = f"{text}\x00{source}\x00{target}\x00{engine}\x00{network_mode}"
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

    def get(self, text: str, source: str, target: str, engine: str, network_mode: str = "") -> Optional[str]:
        """Возвращает кэшированный перевод или None.

        network_mode участвует в ключе: offline_strict-запрос не получит
        online_opt_in-результат из кэша (W1313 F1 HIGH).
        """
        key = _make_key(text, source, target, engine, network_mode)
        with self._lock:
            value = self._cache.get(key)
            if value is None:
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def put(self, text: str, source: str, target: str, engine: str, result: str, network_mode: str = "") -> None:
        """Сохраняет результат перевода в кэш и персистирует на диск.

        network_mode участвует в ключе: разные сетевые режимы → разные записи
        (W1313 F1 HIGH).
        """
        key = _make_key(text, source, target, engine, network_mode)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = result
            self._cache.move_to_end(key)
            # Вытесняем старые записи
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
        self._persist()

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
        self._persist()

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

    def _persist(self) -> None:
        """Записывает кэш на диск (без lock — вызывается после release)."""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with self._lock:
                snapshot = dict(self._cache)
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False)
            os.replace(tmp_path, self._path)
        except Exception as exc:
            logger.warning("Не удалось сохранить translation_cache.json: %s", exc)
