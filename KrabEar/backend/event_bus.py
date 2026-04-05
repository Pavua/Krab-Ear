"""In-process event bus для Krab Ear.

Реализует простой pub/sub через очереди Python. Каждый SSE-подписчик
регистрирует свою очередь; при эмите события оно попадает во все активные очереди.

Поддерживаемые события:
- stt.completed  — транскрибация завершена успешно
- stt.failed     — транскрибация завершилась с ошибкой
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Iterator

logger = logging.getLogger("KrabEar.Backend.EventBus")

# Таймаут ожидания события в SSE-итераторе (секунды).
# После него отправляется keepalive-комментарий, чтобы соединение не закрылось.
_SSE_POLL_TIMEOUT_SEC = 15.0
_QUEUE_MAXSIZE = 64


class EventBus:
    """Потокобезопасный in-process event bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[dict[str, Any] | None]] = []

    def subscribe(self) -> queue.Queue[dict[str, Any] | None]:
        """Регистрирует нового подписчика и возвращает его очередь.

        Возвращённую очередь нужно передать в unsubscribe() при отключении.
        None в очереди означает сигнал завершения.
        """
        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers.append(q)
        logger.debug("EventBus: новый подписчик, всего %d", len(self._subscribers))
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any] | None]) -> None:
        """Удаляет подписчика из списка активных."""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
        logger.debug("EventBus: подписчик отключён, осталось %d", len(self._subscribers))

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Публикует событие всем активным подписчикам.

        Если очередь подписчика переполнена — пропускаем его (не блокируем pipeline).
        """
        event = {
            "type": event_type,
            "ts": time.time(),
            "payload": payload,
        }
        with self._lock:
            active = list(self._subscribers)
        dropped = 0
        for q in active:
            try:
                q.put_nowait(event)
            except queue.Full:
                dropped += 1
        if dropped:
            logger.warning("EventBus: %d подписчик(ов) пропустили событие %s (очередь полна)", dropped, event_type)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def sse_stream(bus: EventBus) -> Iterator[str]:
    """Генератор Server-Sent Events для одного HTTP-клиента.

    Подписывается на шину, конвертирует события в SSE-формат и отписывается
    при разрыве соединения.
    """
    q = bus.subscribe()
    try:
        while True:
            try:
                event = q.get(timeout=_SSE_POLL_TIMEOUT_SEC)
            except queue.Empty:
                # Keepalive — браузеры и прокси закрывают idle-соединения
                yield ": keepalive\n\n"
                continue

            if event is None:
                # Сигнал завершения от сервера
                break

            yield f"event: {event['type']}\ndata: {json.dumps(event['payload'])}\n\n"
    finally:
        bus.unsubscribe(q)


# Глобальный синглтон шины — используется и в service.py, и в rest_server.py
bus = EventBus()
