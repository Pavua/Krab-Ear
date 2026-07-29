"""EventBridge — доставляет события из IPC-процесса в REST-процесс (Krab Ear).

Прод = два процесса (`service.py` IPC + `rest_server.py` :5005) с РАЗДЕЛЬНЫМИ
module-level шинами `backend/event_bus.py::bus` (два разных Python-интерпретатора,
общий исходный код `EventBus()`, НЕ общая память). События, эмитнутые в
IPC-процессе, никогда не доходили до SSE/WS-подписчиков REST-процесса — жертвы:
wake word / krab_error (обходной путь — IPC-поллинг), rewriter_recovered
(flash-green мёртв), live_subs.result агентским путём (см. Задача 1 плана волны;
живой аудит 2026-07-07 подтвердил гэп на throwaway IPC+REST паре).

Спека: docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.1.

Архитектура: EventBridge подключается как push-листенер (event_bus.add_listener)
к ЛОКАЛЬНОЙ (IPC-процесса) шине. on_event() — неблокирующий (контракт
add_listener): кладёт готовый конверт в bounded deque(maxlen=256, drop-oldest)
и будит daemon sender-тред. Sender-тред батчами (<=20) POST-ит на
127.0.0.1:{settings.REST_SERVER_PORT}/internal/event с bridge-токеном; при
недоступности REST — экспоненциальный backoff 1->30s, WARN только по смене
состояния (up/down), эмиттеры никогда не блокируются. Однонаправленно
(IPC -> REST) — см. спека §2 "вариант А".

Stale-TTL (поправка контролёра №1, 2026-07-07): перед включением конверта в
батч sender сверяет его возраст (now - ts); конверты старше MAX_EVENT_AGE_SEC
отбрасываются (счётчик dropped_stale) вместо отправки задним числом после
долгого даунтайма REST — иначе восстановление после даунтайма выплюнуло бы
burst стухших UI-событий (старые субтитры поверх свежих). Невалидный/
отсутствующий ts трактуется как "свежий" (fail-open — не терять события
из-за формата).
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Settings

logger = logging.getLogger("KrabEar.Backend.EventBridge")

# --- Константы (спека §2.1, фиксированы — НЕ настройки) ---------------------
QUEUE_MAXLEN = 256               # bounded deque, drop-oldest при переполнении
BATCH_MAX = 20                   # максимум конвертов за один POST
POST_TIMEOUT_SEC = 2.0           # requests timeout на один POST
MAX_EVENT_AGE_SEC = 30.0         # stale-TTL при отправке (поправка контролёра №1)
BACKOFF_MIN_SEC = 1.0            # стартовый backoff при недоступности REST
BACKOFF_MAX_SEC = 30.0           # потолок backoff
# Верхняя граница ожидания в sender-цикле (держит stop()/backoff отзывчивыми;
# будится немедленно по wake_event.set() из on_event(), не влияет на задержку).
SENDER_POLL_SEC = 1.0

EVENT_BRIDGE_TOKEN_FILENAME = "event_bridge_token"
_TOKEN_BYTES = 32                # secrets.token_hex(32) -> 64 hex-символа


# ---------------------------------------------------------------------------
# Token file helpers — используются И EventBridge (IPC-сторона, создаёт при
# отсутствии), И rest_server.py (REST-сторона, ТОЛЬКО читает — никогда не
# создаёт, спека §2.2: порядок старта процессов произволен).
# ---------------------------------------------------------------------------

def read_bridge_token(data_dir: Path | str) -> str | None:
    """Читает токен моста, НЕ создавая файл. None если отсутствует/пуст/битый.

    Вызывается REST-стороной лениво на первый запрос — REST может стартовать
    раньше IPC-процесса, который единственный создаёт файл.
    """
    token_path = Path(data_dir) / EVENT_BRIDGE_TOKEN_FILENAME
    try:
        content = token_path.read_text(encoding="utf-8").strip()
        return content or None
    except Exception:
        return None


def _load_or_create_token(data_dir: Path) -> str:
    """Читает токен из <data_dir>/event_bridge_token или создаёт новый.

    Вызывается ТОЛЬКО из EventBridge.start() (IPC-сторона). Атомарная запись
    (tempfile + rename), права 0600 — паттерн идентичен
    backend/privacy_audit.py::_load_or_create_key.
    """
    existing = read_bridge_token(data_dir)
    if existing:
        return existing

    token_path = data_dir / EVENT_BRIDGE_TOKEN_FILENAME
    token = secrets.token_hex(_TOKEN_BYTES)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=".event_bridge_token.")
        try:
            os.write(fd, token.encode("utf-8"))
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_path, token_path)
        token_path.chmod(0o600)
    except Exception:
        logger.exception("EventBridge: не удалось записать event_bridge_token")
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
    return token


def _default_post_fn(url: str, payload: dict[str, Any], token: str, timeout: float) -> bool:
    """Реальный сетевой POST. True на 2xx, False на любой сбой (никогда не бросает).

    Тесты ВСЕГДА инжектируют свой post_fn и никогда не проходят через эту
    функцию (спека требует "без сети" в юнит-тестах).
    """
    import requests  # локальный импорт — держим event_bridge.py дешёвым при disabled

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def _envelope_age_sec(envelope: dict[str, Any], now: datetime) -> float:
    """Возраст конверта в секундах (now - ts). Fail-open: невалидный/отсутствующий
    ts -> возраст 0.0 (считается свежим — не терять события из-за формата)."""
    ts_str = envelope.get("ts")
    if not ts_str:
        return 0.0
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()
    except Exception:
        return 0.0


class EventBridge:
    """Мост IPC -> REST: подписывается на локальную шину, батчами POST-ит на REST.

    Конструктор НЕ запускает поток — вызывающая сторона обязана вызвать start()
    (симметрично stop()), как DiskSpaceMonitor/LLMHttpProbe.
    """

    def __init__(
        self,
        settings: "Settings",
        data_dir: Path,
        post_fn: Callable[[str, dict[str, Any], str, float], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._post_fn = post_fn or _default_post_fn

        # M2: в слитом процессе (REST внутри backend) шина ОДНА — мост создал бы
        # echo: событие ушло бы на /internal/event и вернулось в ту же шину.
        # Поэтому in-process режим выключает мост так же жёстко, как killswitch.
        self._enabled = bool(getattr(settings, "EVENT_BRIDGE_ENABLED", True)) and not bool(
            getattr(settings, "REST_IN_PROCESS_ENABLED", False)
        )
        self._rest_port = int(getattr(settings, "REST_SERVER_PORT", 5005))
        self._url = f"http://127.0.0.1:{self._rest_port}/internal/event"

        self._token: str | None = None  # ленивое создание — только в start()

        self._queue: deque[dict[str, Any]] = deque(maxlen=QUEUE_MAXLEN)
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._state = "disabled" if not self._enabled else "unknown"  # unknown|up|down|disabled
        self._current_backoff = BACKOFF_MIN_SEC
        self._next_retry_ts = 0.0

        self._sent_count = 0
        self._dropped_count = 0
        self._dropped_stale_count = 0
        self._failed_count = 0

    # ------------------------------------------------------------------
    # EventBus listener (add_listener contract: неблокирующий, без I/O)
    # ------------------------------------------------------------------

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Push-листенер EventBus. НЕ делает I/O — только deque.append + wake."""
        if not self._enabled:
            return
        envelope = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": payload,
        }
        with self._lock:
            if len(self._queue) >= self._queue.maxlen:
                self._dropped_count += 1
            self._queue.append(envelope)
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запускает daemon sender-тред. Idempotent. No-op если EVENT_BRIDGE_ENABLED=False."""
        if not self._enabled:
            logger.info("EventBridge отключён (EVENT_BRIDGE_ENABLED=False)")
            return
        if self._token is None:
            self._token = _load_or_create_token(self._data_dir)
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="EventBridge", daemon=True)
        self._thread.start()
        logger.info("EventBridge запущен (url=%s)", self._url)

    def stop(self) -> None:
        """Graceful shutdown: дожидается завершения потока (до 5с). Idempotent."""
        self._stop_event.set()
        self._wake_event.set()  # немедленно будим поток, не дожидаясь SENDER_POLL_SEC
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.debug("EventBridge остановлен")

    # ------------------------------------------------------------------
    # Sender loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=SENDER_POLL_SEC)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            now = time.monotonic()
            if self._state == "down" and now < self._next_retry_ts:
                continue
            self._drain_and_send()

    def _drain_and_send(self) -> None:
        """Отправляет до BATCH_MAX свежих конвертов. Peek (не pop) до подтверждения
        успеха — на неудаче батч ОСТАЁТСЯ в очереди для следующей попытки
        (спека: эмиттеры не блокируются, но события, которые ещё В ОЧЕРЕДИ,
        не считаются "потерянными при даунтайме" — потеря только через
        drop-oldest при переполнении в on_event()).

        Stale-TTL (поправка контролёра №1): ПЕРЕД формированием батча очередь
        сканируется целиком и конверты старше MAX_EVENT_AGE_SEC вычищаются
        безусловно (не отправляются НИКОГДА, даже при следующей попытке) —
        иначе долгий даунтайм REST привёл бы к burst стухших событий при
        восстановлении."""
        now = datetime.now(timezone.utc)
        with self._lock:
            fresh: deque[dict[str, Any]] = deque(maxlen=self._queue.maxlen)
            stale_dropped = 0
            for envelope in self._queue:
                if _envelope_age_sec(envelope, now) > MAX_EVENT_AGE_SEC:
                    stale_dropped += 1
                else:
                    fresh.append(envelope)
            if stale_dropped:
                self._queue.clear()
                self._queue.extend(fresh)
                self._dropped_stale_count += stale_dropped
                logger.warning(
                    "EventBridge: %d стухших конвертов (> %.0fс) отброшены при отправке",
                    stale_dropped, MAX_EVENT_AGE_SEC,
                )
            batch = list(self._queue)[:BATCH_MAX]
        if not batch:
            return
        ok = self._post_fn(self._url, {"events": batch}, self._token or "", POST_TIMEOUT_SEC)
        if ok:
            with self._lock:
                for _ in range(len(batch)):
                    if self._queue:
                        self._queue.popleft()
                self._sent_count += len(batch)
            self._current_backoff = BACKOFF_MIN_SEC
            self._next_retry_ts = 0.0
            self._set_state("up")
        else:
            with self._lock:
                self._failed_count += len(batch)
            self._next_retry_ts = time.monotonic() + self._current_backoff
            self._current_backoff = min(self._current_backoff * 2, BACKOFF_MAX_SEC)
            self._set_state("down")

    def _set_state(self, new_state: str) -> None:
        with self._lock:
            old_state = self._state
            if old_state == new_state:
                return
            self._state = new_state
        if new_state == "down":
            logger.warning(
                "EventBridge: REST недоступен (%s) — backoff=%.0fs", self._url, self._current_backoff
            )
        elif new_state == "up" and old_state != "unknown":
            logger.info("EventBridge: REST снова доступен (%s)", self._url)

    # ------------------------------------------------------------------
    # Diagnostics (get_diagnostics.event_bridge, Задача 5)
    # ------------------------------------------------------------------

    def get_diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "state": self._state,
                "queue_depth": len(self._queue),
                "sent": self._sent_count,
                "dropped": self._dropped_count,
                "dropped_stale": self._dropped_stale_count,
                "failed": self._failed_count,
            }
