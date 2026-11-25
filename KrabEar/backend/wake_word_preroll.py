"""Короткое кольцо PCM для «pre-roll» после срабатывания слова-пробуждения.

Зачем: от конца слова «Краб» до старта записи агентом проходит ~1,3–1,7 с (хвост VAD, распознавание, опрос агента,
старт сессии разговора), и первые слова команды терялись. Адаптер держит последние секунды звука с тапа микрофона и
по одноразовому IPC отдаёт агенту кусок «с момента после слова».

Приватность (инварианты, тесты ``test_wake_word_preroll_2026_11_25.py``):
  * только память процесса, на диск и в лог не пишется; кольцо принадлежит одной сессии прослушивания и исчезает
    вместе с ней (выход цикла, смена ревизии политики захвата);
  * ёмкость ограничена (последние ``max_samples``), старое звук вытесняется;
  * отдаётся одноразово и только по свежему срабатыванию (TTL, проверка ревизии — в адаптере).
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


class PreRollBuffer:
    """Потокобезопасное кольцо int16-mono. Позиции — абсолютные индексы отсчётов с момента создания."""

    def __init__(self, max_samples: int) -> None:
        self._max = max(1, int(max_samples))
        self._lock = threading.Lock()
        self._chunks: deque[tuple[int, bytes]] = deque()  # (абсолютный индекс первого отсчёта, little-endian int16)
        self._held = 0
        self._total = 0

    @property
    def total(self) -> int:
        """Сколько отсчётов прошло через кольцо за всё время (абсолютная позиция конца потока)."""
        with self._lock:
            return self._total

    def append(self, chunk: Any) -> int:
        """Добавляет чанк; возвращает абсолютную позицию конца потока после добавления."""
        import numpy as np

        data = np.asarray(chunk, dtype="<i2").reshape(-1).tobytes()
        n = len(data) // 2
        with self._lock:
            self._chunks.append((self._total, data))
            self._held += n
            self._total += n
            while self._held - len(self._chunks[0][1]) // 2 >= self._max and len(self._chunks) > 1:
                self._held -= len(self._chunks.popleft()[1]) // 2
            return self._total

    def slice_from(self, start: int, max_samples: int) -> bytes:
        """Звук от абсолютной позиции ``start`` до конца потока, не длиннее ``max_samples`` (берётся НАЧАЛО куска:
        начало команды важнее её хвоста). Якорь старее кольца подтягивается к его началу; из будущего — пусто."""
        with self._lock:
            if not self._chunks:
                return b""
            oldest = self._chunks[0][0]
            begin = max(int(start), oldest)
            if begin >= self._total:
                return b""
            joined = b"".join(data for _, data in self._chunks)
        offset = (begin - oldest) * 2
        return joined[offset:offset + max(0, int(max_samples)) * 2]

    def clear(self) -> None:
        """Забывает весь звук (счётчик позиций продолжается: якоря остаются согласованными)."""
        with self._lock:
            self._chunks.clear()
            self._held = 0
