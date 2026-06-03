"""Персистентный LRU-кэш переводов для Krab Ear.

Хранит результаты перевода на диске (translation_cache.json) и в памяти
(OrderedDict, LRU-вытеснение). Ключ — хэш (text + source + target + engine).
Максимум 5000 записей; при превышении удаляются самые старые.

Защита от disk-DoS (wave-23):
  * per-value byte cap (``_MAX_VALUE_BYTES`` = 64 KiB) — слишком большие
    значения не кэшируются (значение — это полностью attacker-controlled
    переведённый текст, проходящий через un-throttled ``translate_selection``).
  * total-bytes ceiling (``_MAX_TOTAL_BYTES``) — суммарный размовый бюджет
    сериализованных значений; вытеснение идёт по байтам И по числу записей.
  * debounced persist (``flush_every``) — каждый ``put`` больше не делает
    полный ``json.dump`` + ``os.fsync`` всего кэша. При ``flush_every > 1``
    запись на диск коалесцируется: дисковый сброс происходит каждые N puts,
    а остаток гарантированно сбрасывается через ``flush()`` / ``close()``
    (вызывается из GracefulShutdownHandler). По умолчанию ``flush_every=1``
    — поведение совместимо со старыми вызывающими (write-on-every-put).
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

# Per-value byte cap: одно значение (переведённый текст) не должно превышать
# этот размер в UTF-8-байтах. Защищает от того, чтобы один гигантский ответ
# раздул файл кэша. 64 KiB с запасом покрывает любой осмысленный перевод.
_MAX_VALUE_BYTES = 64 * 1024

# Total-bytes ceiling: суммарный бюджет сериализованных значений (UTF-8).
# 5000 записей × 64 KiB худший случай = 320 MiB — отрезаем намного раньше.
# Вытеснение по байтам срабатывает раньше, чем по числу записей, если
# значения крупные.
_MAX_TOTAL_BYTES = 32 * 1024 * 1024  # 32 MiB


def _make_key(text: str, source: str, target: str, engine: str) -> str:
    """Возвращает SHA-256 hex-хэш параметров перевода."""
    raw = f"{text}\x00{source}\x00{target}\x00{engine}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TranslationCache:
    """LRU-кэш переводов с персистентностью на диск.

    Потокобезопасен (threading.Lock).
    """

    def __init__(
        self,
        data_dir: str,
        max_entries: int = _MAX_ENTRIES,
        max_value_bytes: int = _MAX_VALUE_BYTES,
        max_total_bytes: int = _MAX_TOTAL_BYTES,
        flush_every: int = 1,
    ) -> None:
        self._data_dir = data_dir
        self._max_entries = max_entries
        self._max_value_bytes = max(1, int(max_value_bytes))
        self._max_total_bytes = max(1, int(max_total_bytes))
        # Дисковый сброс коалесцируется: писать на диск каждые flush_every puts.
        # flush_every=1 (по умолчанию) → запись на каждый put (старое поведение).
        self._flush_every = max(1, int(flush_every))
        self._cache: OrderedDict[str, str] = OrderedDict()
        # Параллельная карта размеров значений (UTF-8-байты) для total-byte учёта.
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        self._hits = 0
        self._misses = 0
        # Счётчик puts с последнего дискового сброса + dirty-флаг для debounce.
        self._puts_since_flush = 0
        self._dirty = False
        self._lock = threading.Lock()
        self._path = os.path.join(data_dir, _CACHE_FILENAME)
        self._load()

    @staticmethod
    def _value_bytes(value: str) -> int:
        """Размер значения в UTF-8-байтах (стоимость хранения)."""
        return len(value.encode("utf-8"))

    def _evict_locked(self) -> None:
        """Вытесняет старые записи, пока не уложимся И в max_entries, И в
        max_total_bytes. Вызывать под self._lock.
        """
        cache = self._cache
        while cache and (
            len(cache) > self._max_entries or self._total_bytes > self._max_total_bytes
        ):
            old_key, _ = cache.popitem(last=False)
            self._total_bytes -= self._sizes.pop(old_key, 0)
        # Защита от рассинхрона счётчика из-за округлений/повреждённой загрузки.
        if self._total_bytes < 0:
            self._total_bytes = 0

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
        """Сохраняет результат перевода в кэш и (де)персистирует на диск.

        Защита от disk-DoS:
          * значения крупнее ``max_value_bytes`` НЕ кэшируются (тихо пропускаются).
          * после вставки выполняется вытеснение по числу записей И по байтам.
          * запись на диск коалесцируется через ``flush_every`` (см. ``_maybe_persist_locked``).
        """
        value_size = self._value_bytes(result)
        # (a) per-value byte cap — слишком большое значение не кэшируем вообще.
        if value_size > self._max_value_bytes:
            logger.debug(
                "translation_cache: значение %d B превышает cap %d B — не кэшируется",
                value_size,
                self._max_value_bytes,
            )
            return

        key = _make_key(text, source, target, engine)
        with self._lock:
            if key in self._cache:
                # Перезапись: вычитаем старый размер перед заменой.
                self._total_bytes -= self._sizes.get(key, 0)
                self._cache.move_to_end(key)
            self._cache[key] = result
            self._sizes[key] = value_size
            self._total_bytes += value_size
            self._cache.move_to_end(key)
            # (b) Вытесняем старые записи по count + total-bytes.
            self._evict_locked()
            # (c) Debounced persist под тем же lock.
            self._maybe_persist_locked(force=False, from_put=True)

    def flush(self) -> None:
        """Принудительно сбрасывает накопленные изменения на диск (если dirty).

        Вызывается из GracefulShutdownHandler, чтобы коалесцированные
        ``put``-ы не потерялись при завершении процесса.
        """
        with self._lock:
            self._maybe_persist_locked(force=True, from_put=False)

    def close(self) -> None:
        """Алиас ``flush()`` — финальный сброс перед завершением."""
        self.flush()

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
            self._sizes.clear()
            self._total_bytes = 0
            self._hits = 0
            self._misses = 0
            # clear — это явная финализирующая операция: пишем немедленно
            # (snapshot пустой dict), сбрасывая dirty/debounce-состояние.
            self._puts_since_flush = 0
            self._dirty = False
            self._persist(snapshot={})

    # ── Private helpers ─────────────────────────────────────────────────

    def _maybe_persist_locked(self, *, force: bool, from_put: bool) -> None:
        """Сбрасывает кэш на диск с debounce. Вызывать ТОЛЬКО под self._lock.

        Логика коалесцирования: каждый ``put`` помечает кэш dirty и считает
        ``_puts_since_flush``. Реальная дисковая запись (``json.dump`` + ``fsync``)
        выполняется, когда:
          * ``force`` (вызов из flush()/close()) и кэш dirty, либо
          * число puts с последнего сброса достигло ``flush_every``.

        При ``flush_every == 1`` запись происходит на каждый put — поведение
        идентично старой реализации (важно для существующих тестов, читающих
        файл сразу после put).

        ``from_put`` отличает вызов из ``put`` (помечает dirty, инкрементит
        счётчик) от форс-сброса (``flush``/``close``), который не должен сам
        по себе делать кэш dirty.
        """
        if from_put:
            self._dirty = True
            self._puts_since_flush += 1
        if not self._dirty:
            # Нечего сбрасывать — не делаем лишний fsync.
            return
        if not force and self._puts_since_flush < self._flush_every:
            # Коалесцируем: оставляем изменения только в памяти.
            return
        # Snapshot снимается здесь, под lock — консистентен с памятью (F5).
        self._persist(snapshot=dict(self._cache))
        self._puts_since_flush = 0
        self._dirty = False

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
                # Восстанавливаем учёт байтов и применяем byte-eviction —
                # защита от заранее раздутого/отравленного файла на диске.
                self._sizes = {}
                self._total_bytes = 0
                for k, v in self._cache.items():
                    size = self._value_bytes(v) if isinstance(v, str) else 0
                    self._sizes[k] = size
                    self._total_bytes += size
                self._evict_locked()
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
