"""KEG2-09: обратимый admission-fence «проверка покоя → остановка backend».

Проблема: ``scripts/safe_backend_restart.command`` делал два IPC-снимка
(запись, встреча) и потом ``launchctl kickstart -k``. Между снимком и kickstart
новая запись/встреча/CallAssist стартовала свободно — диктовка терялась
(инцидент 2026-07-22). Снимок не барьер.

Модель:

* Каждый путь старта сессии (``start_recording``, ``meeting_start``,
  ``start_call_assist``) берёт **допуск** (:meth:`MaintenanceFence.admit`) ДО
  любого побочного эффекта и отпускает его, когда старт завершён (успешно или
  нет). Пока допуск выдан, старт считается «в полёте».
* :meth:`MaintenanceFence.close_if_quiet` атомарен относительно допусков:
  под внутренним локом проверяет «нет допусков в полёте» и переводит fence в
  ``closing`` — новые допуски с этого момента ЖДУТ (не проходят). Затем, уже
  без лока, выполняется проба активности (запись/встреча/CallAssist). Проба
  idle → ``closed`` на ``lease_sec``; busy/unknown/ошибка → назад в ``open``,
  ждущие допуски проходят.
* ``closed`` обратим: :meth:`reopen` по токену или автоматически по истечении
  lease (скрипт мог упасть между close и kickstart — запись не должна
  оставаться запрещённой навсегда). Новый процесс backend стартует с ``open``.

Инварианты (проверяются тестами из ДРУГОГО потока):

1. Если ``close_if_quiet`` вернул ``closed``, ни один старт, допущенный после
   этого, не проходит до reopen/истечения lease.
2. Старт, допущенный ДО close, делает close невозможным (``busy``), пока не
   отпущен; после отпускания его сессия уже видна пробе.
3. Лок fence НЕ удерживается во время пробы и во время самого старта —
   нет инверсии с локами сервисов (проба обязана быть неблокирующей).

Вложенный допуск (встреча → ``RecordingCoreService.handle_start_recording``)
безопасен: пока внешний допуск выдан, fence не может стать ``closing``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("KrabEar.Backend.MaintenanceFence")

#: Lease по умолчанию (согласовано 24.11: 60 с).
DEFAULT_LEASE_SEC = 60.0
#: Границы lease, принимаемого по IPC.
MIN_LEASE_SEC = 5.0
MAX_LEASE_SEC = 600.0
#: Сколько допуск ждёт завершения пробы в состоянии ``closing``.
DEFAULT_ADMIT_WAIT_SEC = 5.0

STATE_OPEN = "open"
STATE_CLOSING = "closing"
STATE_CLOSED = "closed"

#: Ответ старта записи при закрытом fence (форма как у ``backend_closing``).
REASON_MAINTENANCE = "maintenance_active"


class MaintenanceFence:
    """Обратимый fence допуска новых сессий на время обслуживания backend."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        admit_wait_sec: float = DEFAULT_ADMIT_WAIT_SEC,
    ) -> None:
        self._clock = clock
        self._admit_wait_sec = float(admit_wait_sec)
        self._cond = threading.Condition(threading.Lock())
        self._state = STATE_OPEN
        self._token: Optional[str] = None
        self._deadline = 0.0
        self._inflight: dict[str, str] = {}  # ticket → kind

    # ------------------------------------------------------------------
    # Допуск стартов
    # ------------------------------------------------------------------

    def _expire_locked(self) -> None:
        if self._state == STATE_CLOSED and self._clock() >= self._deadline:
            logger.warning("maintenance fence: lease истёк — допуск снова открыт")
            self._state = STATE_OPEN
            self._token = None
            self._cond.notify_all()

    def admit(self, kind: str) -> Optional[str]:
        """Выдать допуск на старт сессии ``kind`` или ``None`` (fence закрыт).

        В состоянии ``closing`` ждёт исхода пробы не дольше ``admit_wait_sec``;
        не дождался — отказ (fail-closed: старт под обслуживанием опаснее
        повторного нажатия).
        """
        deadline = time.monotonic() + self._admit_wait_sec
        with self._cond:
            while True:
                self._expire_locked()
                if self._state == STATE_OPEN:
                    ticket = uuid.uuid4().hex
                    self._inflight[ticket] = str(kind)
                    return ticket
                if self._state == STATE_CLOSED:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def release(self, ticket: Optional[str]) -> None:
        """Отпустить допуск (идемпотентно)."""
        if ticket is None:
            return
        with self._cond:
            self._inflight.pop(ticket, None)
            self._cond.notify_all()

    @contextmanager
    def admission(self, kind: str) -> Iterator[bool]:
        """``with fence.admission("recording") as admitted: ...``.

        Допуск отпускается после выхода из блока — к этому моменту успешный
        старт уже опубликовал своё состояние и виден пробе.
        """
        ticket = self.admit(kind)
        try:
            yield ticket is not None
        finally:
            self.release(ticket)

    # ------------------------------------------------------------------
    # Закрытие / открытие
    # ------------------------------------------------------------------

    def close_if_quiet(
        self,
        probe: Callable[[], list[str]],
        lease_sec: float = DEFAULT_LEASE_SEC,
    ) -> dict[str, Any]:
        """Атомарно «проверить покой и закрыть допуск».

        ``probe()`` возвращает список причин занятости (пустой = idle). Проба
        обязана быть неблокирующей; исключение = состояние неизвестно = отказ.
        """
        lease = _clamp_lease(lease_sec)
        with self._cond:
            self._expire_locked()
            if self._state != STATE_OPEN:
                return {"ok": False, "reason": "already_closed" if self._state == STATE_CLOSED
                        else "closing_in_progress", "busy_reasons": []}
            if self._inflight:
                kinds = sorted({f"starting_{k}" for k in self._inflight.values()})
                return {"ok": False, "reason": "busy", "busy_reasons": kinds}
            self._state = STATE_CLOSING
        try:
            reasons = probe()
            if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
                raise TypeError("probe вернула не список строк")
        except Exception:
            logger.warning("maintenance fence: проба активности не удалась — отказ", exc_info=True)
            reasons = ["activity_unknown"]
        with self._cond:
            if reasons:
                self._state = STATE_OPEN
                self._cond.notify_all()
                return {"ok": False, "reason": "busy", "busy_reasons": sorted(set(reasons))}
            self._state = STATE_CLOSED
            self._token = uuid.uuid4().hex
            self._deadline = self._clock() + lease
            self._cond.notify_all()
            logger.warning("maintenance fence: допуск закрыт на %.0f с", lease)
            return {"ok": True, "token": self._token, "lease_sec": lease}

    def reopen(self, token: Any) -> bool:
        """Открыть допуск по токену закрытия. Чужой/устаревший токен — False."""
        with self._cond:
            self._expire_locked()
            if self._state != STATE_CLOSED or not isinstance(token, str) or token != self._token:
                return False
            self._state = STATE_OPEN
            self._token = None
            self._cond.notify_all()
            logger.warning("maintenance fence: допуск открыт вручную")
            return True

    def status(self) -> dict[str, Any]:
        with self._cond:
            self._expire_locked()
            remaining = max(0.0, self._deadline - self._clock()) if self._state == STATE_CLOSED else 0.0
            return {"state": self._state, "inflight": len(self._inflight),
                    "lease_remaining_sec": round(remaining, 1)}


def _clamp_lease(value: Any) -> float:
    try:
        lease = float(value)
    except (TypeError, ValueError):
        return DEFAULT_LEASE_SEC
    if lease != lease or lease in (float("inf"), float("-inf")):
        return DEFAULT_LEASE_SEC
    return max(MIN_LEASE_SEC, min(MAX_LEASE_SEC, lease))


def fence_of(service: Any) -> Optional[MaintenanceFence]:
    """Late-injected fence сервиса или None (узкие тесты без BackendService)."""
    fence = getattr(service, "_maintenance_fence", None)
    return fence if isinstance(fence, MaintenanceFence) else None


@contextmanager
def optional_admission(service: Any, kind: str) -> Iterator[bool]:
    """Допуск через fence сервиса; без fence — всегда допущен (прежнее поведение)."""
    fence = fence_of(service)
    if fence is None:
        yield True
        return
    with fence.admission(kind) as admitted:
        yield admitted


def activity_busy_reasons(*components: tuple[str, Any]) -> list[str]:
    """Свести неблокирующие ``activity_snapshot`` компонентов в причины занятости.

    ``unknown``/ошибка/отсутствующий компонент — тоже причина (fail-closed):
    fence закрывается только при ДОКАЗАННОМ idle всех путей.
    """
    from backend.activity_diagnostics import component_activity

    reasons: list[str] = []
    for name, service in components:
        if service is None:
            reasons.append(f"{name}_unavailable")
            continue
        snap = component_activity(service)
        state = snap.get("state")
        if state == "idle" and snap.get("known") is True:
            continue
        reasons.append(f"{name}_{'busy' if state == 'busy' else 'unknown'}")
    return reasons
