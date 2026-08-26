"""Per-request бюджеты STT (спека 2026-08-26-stt-timeout-budgets-design.md).

Инцидент 2026-08-26: TRANSCRIBE_TIMEOUT_SEC=3600 применялся одинаково к
4.7-секундной диктовке и к часовому импорту — абандоненный поток 2 часа
удерживал MLX-локи. Здесь бюджет попытки масштабируется от длительности
аудио и профиля пути, а запрос получает общий дедлайн.

ContextVar, а не поле AudioEngine: engine — один экземпляр на все IPC-треды
(per-request состояние в общем объекте = last-writer-wins). ContextVar НЕ
наследуется новыми тредами — scope обязан открываться в том же треде, где
исполняется STT; для внешних пулов (REST) есть call_in_scope().
"""
from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator

logger = logging.getLogger("KrabEar.Core.STTBudget")

INTERACTIVE = "interactive"
BATCH = "batch"

# Попытка с остатком меньше этого заведомо бесполезна (одна загрузка модели
# дольше), но занимает GPU — каскад прерывается, а не сабмитит её.
MIN_USEFUL_ATTEMPT_SEC = 5.0

# §4.8: внешний таймаут adapter-ветки не смеет быть короче внутренних
# таймаутов GigaAM-subprocess (120 с shortform / 180 с load) — иначе
# брошенный subprocess осиротеет с ~1.5 ГБ модели на GPU.
ADAPTER_MIN_BUDGET_SEC = 200.0

# Единственный источник границ (min, max, default); settings_validator
# дублирует литералы в _RANGE_FIELDS, а тест волны сверяет равенство —
# синхронизация тестом вместо прод-импорта backend← core.
KNOB_BOUNDS: dict[str, tuple[float, float, float]] = {
    "stt_timeout_overhead_sec": (5.0, 600.0, 90.0),
    "stt_timeout_interactive_factor": (0.5, 60.0, 3.0),
    "stt_timeout_batch_factor": (0.5, 120.0, 6.0),
    "stt_timeout_interactive_max_sec": (30.0, 7200.0, 1800.0),
    "stt_timeout_batch_max_sec": (60.0, 21600.0, 3600.0),
    "stt_timeout_request_attempts": (1.0, 10.0, 4.0),
}


@dataclass(frozen=True)
class STTBudget:
    """Снапшот бюджета одного STT-запроса (immutable, живёт в ContextVar)."""

    profile: str
    deadline_monotonic: float | None
    overhead_sec: float
    interactive_factor: float
    batch_factor: float
    interactive_max_sec: float
    batch_max_sec: float


_current: ContextVar[STTBudget | None] = ContextVar("stt_budget", default=None)


def _read_knob(
    settings_get: Callable[[str, Any], Any] | None, key: str
) -> float:
    """Одно значение настройки с клампом к KNOB_BOUNDS; мусор/NaN → default."""
    lo, hi, default = KNOB_BOUNDS[key]
    if settings_get is None:
        return default
    try:
        value = float(settings_get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, lo), hi)


def _build_budget(
    profile: str,
    settings_get: Callable[[str, Any], Any] | None,
) -> STTBudget:
    prof = BATCH if profile == BATCH else INTERACTIVE
    return STTBudget(
        profile=prof,
        deadline_monotonic=None,
        overhead_sec=_read_knob(settings_get, "stt_timeout_overhead_sec"),
        interactive_factor=_read_knob(
            settings_get, "stt_timeout_interactive_factor"
        ),
        batch_factor=_read_knob(settings_get, "stt_timeout_batch_factor"),
        interactive_max_sec=_read_knob(
            settings_get, "stt_timeout_interactive_max_sec"
        ),
        batch_max_sec=_read_knob(settings_get, "stt_timeout_batch_max_sec"),
    )


def _attempt_budget(
    budget: STTBudget, audio_duration_sec: float | None
) -> float:
    """§4.2: overhead + duration×factor, потолок профиля; duration неизвестна
    → потолок профиля (fail-open внутри профиля, но не в час)."""
    if budget.profile == BATCH:
        factor, cap = budget.batch_factor, budget.batch_max_sec
    else:
        factor, cap = budget.interactive_factor, budget.interactive_max_sec
    try:
        duration = float(audio_duration_sec)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return cap
    if not math.isfinite(duration) or duration <= 0.0:
        return cap
    return min(budget.overhead_sec + duration * factor, cap)


@contextmanager
def stt_budget_scope(
    profile: str,
    *,
    settings_get: Callable[[str, Any], Any] | None = None,
    audio_duration_sec: float | None = None,
    deadline_sec: float | None = None,
) -> Iterator[STTBudget]:
    """Открыть бюджет-контекст одного STT-запроса В ТЕКУЩЕМ ТРЕДЕ.

    deadline_sec не задан → вычисляется как attempt_budget × request_attempts
    (§4.6); задан явно (REST deadline_sec, W2c) — используется как есть.
    """
    base = _build_budget(profile, settings_get)
    attempt_sec = _attempt_budget(base, audio_duration_sec)
    if deadline_sec is None:
        deadline_sec = attempt_sec * _read_knob(
            settings_get, "stt_timeout_request_attempts"
        )
    budget = replace(
        base, deadline_monotonic=time.monotonic() + float(deadline_sec)
    )
    logger.info(
        "stt_budget: scope opened",
        extra={
            "profile": budget.profile,
            "audio_duration_sec": audio_duration_sec,
            "attempt_budget_sec": round(attempt_sec, 1),
            "deadline_sec": round(float(deadline_sec), 1),
        },
    )
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


def current_profile() -> str:
    budget = _current.get()
    return budget.profile if budget is not None else INTERACTIVE


def remaining_sec() -> float | None:
    """Остаток дедлайна запроса; None — дедлайн не установлен (нет scope)."""
    budget = _current.get()
    if budget is None or budget.deadline_monotonic is None:
        return None
    return budget.deadline_monotonic - time.monotonic()


def budget_exhausted(min_useful_sec: float = MIN_USEFUL_ATTEMPT_SEC) -> bool:
    rem = remaining_sec()
    return rem is not None and rem <= float(min_useful_sec)


def resolve_attempt_timeout_sec(audio_duration_sec: float | None) -> float:
    """Таймаут ОДНОЙ попытки STT: формула §4.2 + клип по остатку дедлайна.

    Нижний floor MIN_USEFUL_ATTEMPT_SEC гарантирует, что future.result
    никогда не получит отрицательный/нулевой таймаут (мгновенный
    TimeoutError неотличим от настоящего зависания).
    """
    budget = _current.get() or _build_budget(INTERACTIVE, None)
    attempt = _attempt_budget(budget, audio_duration_sec)
    rem = remaining_sec()
    if rem is not None:
        attempt = min(attempt, rem)
    return max(attempt, MIN_USEFUL_ATTEMPT_SEC)


def timeout_blacklist_allowed() -> bool:
    """§4.7: TimeoutError при исчерпанном бюджете ЗАПРОСА не доказывает
    нездоровье модели — блэклист (_unavailable_models, TTL 300 с) разрешён
    только когда попытка располагала полным бюджетом."""
    return not budget_exhausted(MIN_USEFUL_ATTEMPT_SEC)


def call_in_scope(
    fn: Callable[..., Any],
    /,
    *args: Any,
    profile: str,
    settings_snapshot: dict[str, Any] | None = None,
    audio_duration_sec: float | None = None,
    deadline_sec: float | None = None,
    **kwargs: Any,
) -> Any:
    """Выполнить fn под бюджет-scope В ТРЕДЕ ВЫЗОВА.

    Назначение — submit во внешний ThreadPoolExecutor (REST): ContextVar не
    наследуется worker-тредом, поэтому scope, открытый вокруг submit, там
    невидим; эта обёртка открывает его уже внутри воркера (§4.1).
    """
    snap = settings_snapshot or {}
    with stt_budget_scope(
        profile,
        settings_get=snap.get if snap else None,
        audio_duration_sec=audio_duration_sec,
        deadline_sec=deadline_sec,
    ):
        return fn(*args, **kwargs)
