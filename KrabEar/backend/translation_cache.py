"""Персистентный LRU-кэш переводов для Krab Ear.

Хранит результаты перевода на диске (translation_cache.json) и в памяти
(OrderedDict, LRU-вытеснение). Ключ — хэш (text + source + target + engine +
network_mode). network_mode включён в ключ чтобы предотвратить возврат
online-результата для offline_strict-запроса (W1313 F1 HIGH — privacy bypass).
Максимум 5000 записей; при превышении удаляются самые старые.

Backward compat: старые ключи без network_mode имеют другой хэш → автоматически
трактуются как cache miss (нет специальной обработки).

Формат на диске v2: {"version": 2, "entries": {...}}.  Файлы v1 (plain dict)
автоматически инвалидируются при загрузке — ключи без network_mode несовместимы
с v2 (W1371 F2).

W938 F2: fh.flush() + os.fsync() перед os.replace гарантируют что данные
попадают на диск до атомарного rename — защита от потери кэша при сбое питания.

W1371 F3 / W938 F5: put() и clear() удерживают lock на всё время persist() —
OrderedDict update и запись .tmp файла атомарны относительно параллельных put()
(устраняет TOCTOU гонку на общем .tmp файле).
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
_CACHE_FORMAT_VERSION = 2


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

    Изменения v2 (W1371 + W938 unified, W1394):
      - _make_key включает network_mode (W1313/W1371 F2).
      - _persist_locked() вызывается ВНУТРИ self._lock из put() и clear()
        — устраняет TOCTOU гонку (W1371 F3 / W938 F5).
      - fh.flush() + os.fsync() перед os.replace — защита от power-loss
        truncated cache (W938 F2).
      - Формат на диске v2: {"version": 2, "entries": {...}} — v1 инвалидируется.
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

        F3/F5 fix: lock удерживается на всё время _persist_locked() —
        обновление OrderedDict и запись .tmp файла атомарны относительно
        параллельных put() вызовов (TOCTOU fix, W1371/W938).
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
            # Персистируем внутри lock — атомарная запись (TOCTOU fix)
            self._persist_locked()

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
            # Персистируем внутри lock — атомарная запись (TOCTOU fix)
            self._persist_locked()

    # ── Private helpers ─────────────────────────────────────────────────

    def _load(self) -> None:
        """Загружает кэш с диска при инициализации.

        Формат v2: {"version": 2, "entries": {...}}.
        Формат v1 (plain dict или version != 2) инвалидируется — ключи без
        network_mode несовместимы с v2 (W1371 F2).
        """
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return
            # v2 format: {"version": 2, "entries": {...}}
            if data.get("version") == _CACHE_FORMAT_VERSION:
                entries = data.get("entries", {})
                if isinstance(entries, dict):
                    items = list(entries.items())[-self._max_entries:]
                    self._cache = OrderedDict(items)
            else:
                # v1 or unknown version — discard (keys are incompatible)
                logger.info(
                    "translation_cache.json версии %s устарел (текущая v%s) — "
                    "кэш сброшен для чистой инвалидации.",
                    data.get("version", "unknown"),
                    _CACHE_FORMAT_VERSION,
                )
        except Exception as exc:
            logger.warning("Не удалось загрузить translation_cache.json: %s", exc)

    def _persist_locked(self) -> None:
        """Записывает кэш на диск атомарно (tmp + fsync + os.replace).

        ДОЛЖЕН вызываться ВНУТРИ self._lock — снимает snapshot без
        повторного захвата lock.

        F2 (W938): fh.flush() + os.fsync() перед os.replace гарантируют
        что данные попадают на диск до атомарного rename — защита от потери
        при сбое питания.

        F3/F5 (W1371/W938): вызывается изнутри with self._lock в put() и
        clear() — устраняет TOCTOU гонку параллельных put() на общем .tmp.
        """
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            snapshot = dict(self._cache)
            payload = {"version": _CACHE_FORMAT_VERSION, "entries": snapshot}
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception as exc:
            logger.warning("Не удалось сохранить translation_cache.json: %s", exc)

    def _persist(self) -> None:
        """Обратная совместимость: захватывает lock и вызывает _persist_locked.

        Для внешних вызовов (legacy code). Не используется внутри класса.
        """
        with self._lock:
            self._persist_locked()
