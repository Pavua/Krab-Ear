"""In-process event bus для Krab Ear.

Реализует простой pub/sub через очереди Python. Каждый SSE-подписчик
регистрирует свою очередь; при эмите события оно попадает во все активные очереди.

Формат событий — унифицированный конверт экосистемы Krab (EVENT_CONTRACT_V1):
  {type: str, ts: ISO 8601 UTC, data: dict}

Поддерживаемые события:
- stt.final      — транскрибация завершена успешно
- stt.failed     — транскрибация завершилась с ошибкой
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from contracts.registry import EventType
from pydantic import BaseModel

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
        # Late-injected EventReplayManager — set after both objects are constructed.
        # When not None, every emitted event is also recorded in the replay ring buffer.
        self._event_replay: Any | None = None
        # Synchronous push-listeners — registered via add_listener().  Unlike SSE
        # subscribers (pull-based queues), listeners are server-side callbacks invoked
        # inline inside emit() with (event_type, payload).  Used to forward lifecycle
        # events to side-channels such as webhook delivery (WebhookManager.fire_webhook).
        # The listener itself must be non-blocking (or dispatch its own work to a thread);
        # emit() runs on the emitting thread (e.g. the STT pipeline), so a slow listener
        # would stall that thread.  WebhookManager.fire_webhook already returns immediately
        # (it submits to a bounded ThreadPoolExecutor), so it is safe to call inline.
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []

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

    def add_listener(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Регистрирует синхронный server-side листенер событий.

        Листенер вызывается внутри emit() с (event_type, payload) для КАЖДОГО
        события. В отличие от SSE-подписчиков (pull-очереди), это push-callback на
        стороне backend — используется, например, для доставки lifecycle-событий
        во внешние webhook-и (WebhookManager.fire_webhook).

        ВАЖНО: callback ДОЛЖЕН быть неблокирующим. emit() выполняется в потоке,
        который публикует событие (например, STT-pipeline), поэтому медленный
        листенер застопорит этот поток. WebhookManager.fire_webhook возвращает
        управление немедленно (отправляет в ThreadPoolExecutor), поэтому безопасен.

        Исключения внутри листенера логируются и проглатываются — один сбойный
        листенер не должен ломать доставку события остальным подписчикам.
        """
        with self._lock:
            self._listeners.append(callback)
        logger.debug("EventBus: зарегистрирован листенер, всего %d", len(self._listeners))

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Публикует событие всем активным подписчикам.

        Если очередь подписчика переполнена — пропускаем его (не блокируем pipeline).
        """
        event = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": payload,
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
        # Forward to event replay ring buffer (late-injected, no-op when None).
        # W1673 F4 LOW: pass the already-computed ts so the replay log carries
        # the exact delivery timestamp, not a second clock read inside record_event.
        if self._event_replay is not None:
            try:
                self._event_replay.record_event(event_type, payload, ts=event["ts"])
            except Exception:
                logger.warning("EventBus: не удалось записать событие %s в EventReplayManager", event_type, exc_info=True)
        # Synchronous push-listeners (e.g. webhook forwarder).  Snapshot under the lock
        # so a concurrent add_listener() during iteration can't mutate the list.
        # Each listener is called defensively — a raising listener must not break the
        # others or the pipeline that emitted the event.
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event_type, payload)
            except Exception:
                logger.warning("EventBus: листенер бросил исключение на событии %s", event_type, exc_info=True)

    def emit_typed(self, event_type: EventType, payload: BaseModel) -> None:
        """Типизированный emit — валидирует payload через Pydantic модель."""
        self.emit(event_type.value, payload.model_dump(mode="json"))

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def broadcast_shutdown_sentinel(self) -> int:
        """Рассылает сигнал завершения (None) всем активным подписчикам.

        Вызывается из GracefulShutdownHandler.shutdown() чтобы SSE/WS-клиенты
        немедленно закрыли соединение вместо ожидания poll-таймаута (до 15 с).

        BUG FIX (W1716): если очередь переполнена (30 Гц audio-level события
        заполняют её за ~2 с), sentinel ранее молча отбрасывался → подписчик
        висел до poll-таймаута (15 с) вместо немедленного отключения.
        Решение: дренируем очередь перед отправкой sentinel — буферизованные
        события не нужны при завершении, sentinel имеет абсолютный приоритет.

        Returns:
            Количество подписчиков, которым был отправлен сигнал.
        """
        with self._lock:
            active = list(self._subscribers)
        sent = 0
        for q in active:
            # Drain buffered events so the sentinel can always fit.
            # At shutdown, pending events are irrelevant; fast disconnect is what matters.
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            try:
                q.put_nowait(None)
                sent += 1
            except queue.Full:
                # Theoretically unreachable after drain, but guard defensively.
                pass
        if sent:
            logger.debug("EventBus: sentinel разослан %d подписчику(-ам)", sent)
        return sent


def sse_stream(bus: EventBus, event_filter: str | None = None) -> Iterator[str]:
    """Генератор Server-Sent Events для одного HTTP-клиента.

    Подписывается на шину, конвертирует события в SSE-формат и отписывается
    при разрыве соединения.

    Args:
        bus: шина событий для подписки.
        event_filter: опциональный фильтр — строка с типами событий через запятую
            (например ``"stt.final,live_subs.result"``). Если ``None`` — все события.
    """
    allowed: set[str] | None = None
    if event_filter is not None:
        parsed = {t.strip() for t in event_filter.split(",") if t.strip()}
        # BUG FIX (W1716): an empty-after-parse filter (e.g. "," or " ") previously
        # produced an empty set, making the guard `event["type"] not in allowed`
        # True for every event → silent blackhole, client received only keepalives.
        # Correct intent: empty/blank filter string = "no filter" (receive all).
        if parsed:
            allowed = parsed
        else:
            logger.warning(
                "EventBus sse_stream: пустой event_filter %r — фильтр игнорируется, "
                "клиент получит все события",
                event_filter,
            )

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

            if allowed is not None and event["type"] not in allowed:
                continue

            yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
    finally:
        bus.unsubscribe(q)


# Глобальный синглтон шины — используется и в service.py, и в rest_server.py
bus = EventBus()
