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
# Maximum concurrent SSE/pull subscribers.  Each subscriber holds a Queue and a
# thread (inside the Flask/gevent SSE handler), so an uncapped subscriber list is
# a thread + memory exhaustion vector under load or during connection-flood attacks.
MAX_SUBSCRIBERS = 100
# Максимальное число синхронных push-листенеров (server-side callbacks).
# Листенеры добавляются только при старте подсистем (WebhookManager и др.) —
# фиксированное число, не per-request. Ограничение защищает от случайного
# накопления при многократных reinit (тесты, hot-reload) без явного remove_listener.
_MAX_LISTENERS = 100


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

        Raises RuntimeError when MAX_SUBSCRIBERS is already reached to prevent
        thread/memory exhaustion under connection floods.

        Note: SSE endpoint authentication (Bearer token) is enforced at the HTTP
        layer in rest_server.py (require_auth decorator) — not inside EventBus.
        EventBus is auth-agnostic; callers must gate access before calling subscribe().
        """
        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        with self._lock:
            current_count = len(self._subscribers)
            if current_count >= MAX_SUBSCRIBERS:
                raise RuntimeError(
                    f"max_subscribers reached ({MAX_SUBSCRIBERS}); "
                    "refusing new SSE connection"
                )
            self._subscribers.append(q)
            new_count = len(self._subscribers)
        logger.debug("EventBus: новый подписчик, всего %d", new_count)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any] | None]) -> None:
        """Удаляет подписчика из списка активных."""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            remaining = len(self._subscribers)
        logger.debug("EventBus: подписчик отключён, осталось %d", remaining)

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

        Ограничение _MAX_LISTENERS: если лимит достигнут — предупреждаем и отклоняем
        (в нормальной работе листенеры добавляются только при старте подсистем).
        """
        with self._lock:
            current_count = len(self._listeners)
            if current_count >= _MAX_LISTENERS:
                logger.warning(
                    "EventBus: отклонён листенер — превышен лимит _MAX_LISTENERS=%d (текущее число: %d)",
                    _MAX_LISTENERS,
                    current_count,
                )
                return
            self._listeners.append(callback)
        logger.debug("EventBus: зарегистрирован листенер, всего %d", len(self._listeners))

    def remove_listener(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Удаляет push-листенер из списка (симметрично add_listener).

        Если callback не найден в списке — операция игнорируется (аналогично unsubscribe).
        """
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass
            remaining = len(self._listeners)
        logger.debug("EventBus: листенер удалён, осталось %d", remaining)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Публикует событие всем активным подписчикам.

        Если очередь подписчика переполнена — пропускаем его (не блокируем pipeline).

        Privacy note (wave-1770 design): EventBus is privacy-agnostic by design —
        it does not have access to settings. CALLERS that emit transcript events
        (stt.partial, stt.final, live_subs.result) MUST check privacy_mode_enabled
        before calling emit(). Current call sites: recording_core_service.py
        (RealtimePartialTranscriber.privacy_getter) and live_subs_service.py.
        Adding a centralized gate here would require settings injection which
        breaks the pub/sub abstraction layer.
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

    def emit_envelope(self, envelope: dict[str, Any]) -> None:
        """Доставляет УЖЕ ГОТОВЫЙ конверт подписчикам (SSE/WS) КАК ЕСТЬ.

        Используется REST-стороной event-моста (backend/event_bridge.py,
        docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.3) для
        ре-эмита событий, доставленных из IPC-процесса. В отличие от emit():
          - НЕ вызывает push-листенеров self._listeners (структурный no-echo
            guard — исходный emit() в IPC-процессе их уже вызвал; повторный
            вызов здесь задвоил бы доставку, например, вебхуков).
          - НЕ перештамповывает envelope["ts"] — конверт передаётся как есть.
          - НЕ пишет в self._event_replay (реплей — забота IPC-процесса,
            где _event_replay реально wired; на REST-стороне он всегда None).

        Args:
            envelope: {"type": str, "ts": str, "data": dict, ...} — форма
                уже провалидирована вызывающей стороной (REST /internal/event).
        """
        if "type" not in envelope:
            logger.warning("EventBus.emit_envelope: конверт без 'type' проигнорирован: %r", envelope)
            return
        with self._lock:
            active = list(self._subscribers)
        dropped = 0
        for q in active:
            try:
                q.put_nowait(envelope)
            except queue.Full:
                dropped += 1
        if dropped:
            logger.warning(
                "EventBus: %d подписчик(ов) пропустили bridged-событие %s (очередь полна)",
                dropped, envelope.get("type"),
            )

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

            # wave-1770 MED: strip CR/LF from event_type to prevent SSE CRLF
            # header injection. All current call sites pass hardcoded strings,
            # but the public emit() API accepts arbitrary strings — future callers
            # forwarding user input would be exploitable without this guard.
            safe_type = event["type"].replace("\r", "").replace("\n", "")
            yield f"event: {safe_type}\ndata: {json.dumps(event['data'])}\n\n"
    finally:
        bus.unsubscribe(q)


# Глобальный синглтон шины — используется и в service.py, и в rest_server.py
bus = EventBus()
