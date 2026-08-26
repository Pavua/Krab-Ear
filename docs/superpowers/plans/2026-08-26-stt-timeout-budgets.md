# Раздельные бюджеты STT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Бюджет STT-попытки масштабируется от длительности аудио и профиля пути (interactive/batch); запрос получает общий дедлайн; таймаут по бюджету не отравляет блэклист моделей.

**Architecture:** Новый модуль `core/stt_budget.py` несёт per-request контекст через `ContextVar` (снапшот настроек + абсолютный дедлайн). Три точки чтения `settings.TRANSCRIBE_TIMEOUT_SEC` в `engine.py` переводятся на `resolve_attempt_timeout_sec(duration)`. Пять прод-путей открывают scope; REST — внутри submitted callable (ContextVar не наследуется worker-тредом).

**Tech Stack:** Python 3.14 (dev venv), unittest.TestCase, AST-контракты (паттерн PR #1953), ubuntu-parity py3.12 без mlx.

**Спека (ground truth):** `docs/superpowers/specs/2026-08-26-stt-timeout-budgets-design.md`. При конфликте плана и спеки — спека главнее, но два зафиксированных отклонения см. Global Constraints.

## Global Constraints

- Ветка: `feat/stt-timeout-budgets` (уже создана, спека закоммичена).
- Запуск тестов: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/<file> -v -p no:cacheprovider` из корня worktree; venv: `source .venv_krab_ear/bin/activate`.
- 🔴 Никакой тест не импортирует `backend.rest_server` и не создаёт `BackendService` — chunk-pollution / teardown-классы из CLAUDE.md. Тесты этой волны обходятся `core.stt_budget`, `core.engine` (лёгкий импорт, MLX за try/except) и AST-разбором файлов без импорта.
- 🔴 Сигнатуры `transcribe`/`_transcribe_with_fallback` НЕ меняются (тест-дабли мокают их фиксированно, `engine.py:1397`).
- Новый код логирует через `logger.info("msg", extra={...})` (CLAUDE.md preferred pattern).
- Дефолты/границы настроек — ровно из спеки §9: overhead 90 (5–600), interactive_factor 3.0 (0.5–60), batch_factor 6.0 (0.5–120), interactive_max 1800 (30–7200), batch_max 3600 (60–21600), request_attempts 4.0 (1–10).
- Константы: `MIN_USEFUL_ATTEMPT_SEC = 5.0`, `ADAPTER_MIN_BUDGET_SEC = 200.0`.
- **Отклонение 1 от спеки (§4.7):** класс-маркер `BudgetExhaustedTimeout` НЕ создаётся — различение «бюджет запроса vs модель нездорова» реализовано функцией `stt_budget.timeout_blacklist_allowed()`; отдельный exception-класс был бы мёртвым кодом.
- **Отклонение 2 от спеки (§4.1-псевдокод):** `resolve_attempt_timeout_sec(audio_duration_sec)` — БЕЗ параметра `settings_get`: значения настроек снапшотятся в `STTBudget` при входе в scope (§4.2 главнее раннего псевдокода §4.1).
- Semantика блэклиста при таймауте: таймаут при остатке дедлайна ≤ 5 с (`budget_exhausted`) → НЕ блэклистим (запросу не хватило времени); таймаут при живом дедлайне или без дедлайна → блэклистим (полный бюджет истёк, watchdog 45 с внутри должен был сработать раньше — модель/стек висит).
- Каждая задача — отдельный коммит с trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: Модуль `core/stt_budget.py`

**Files:**
- Create: `KrabEar/core/stt_budget.py`
- Test: `KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py` (класс `BudgetFormulaTests`, `BudgetScopeTests`)

**Interfaces:**
- Produces (все последующие задачи используют именно эти имена):
  - `stt_budget.INTERACTIVE = "interactive"`, `stt_budget.BATCH = "batch"`
  - `stt_budget.MIN_USEFUL_ATTEMPT_SEC: float = 5.0`
  - `stt_budget.ADAPTER_MIN_BUDGET_SEC: float = 200.0`
  - `stt_budget.KNOB_BOUNDS: dict[str, tuple[float, float, float]]` — `{key: (min, max, default)}`, 6 ключей из спеки §9
  - `stt_budget.STTBudget` — frozen dataclass: `profile, deadline_monotonic, overhead_sec, interactive_factor, batch_factor, interactive_max_sec, batch_max_sec`
  - `stt_budget.stt_budget_scope(profile, *, settings_get=None, audio_duration_sec=None, deadline_sec=None)` — contextmanager
  - `stt_budget.current_profile() -> str`
  - `stt_budget.remaining_sec() -> float | None`
  - `stt_budget.budget_exhausted(min_useful_sec: float = MIN_USEFUL_ATTEMPT_SEC) -> bool`
  - `stt_budget.resolve_attempt_timeout_sec(audio_duration_sec: float | None) -> float`
  - `stt_budget.timeout_blacklist_allowed() -> bool`
  - `stt_budget.call_in_scope(fn, /, *args, profile, settings_snapshot=None, audio_duration_sec=None, deadline_sec=None, **kwargs)` — обёртка для submit во внешний пул (REST, Task 6)

- [ ] **Step 1: Написать RED-тесты формулы и scope**

Создать `KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py`:

```python
"""Раздельные бюджеты STT (спека 2026-08-26-stt-timeout-budgets-design.md).

Инцидент-источник: 2026-08-26 04:21–06:21 — 4.71 с аудио держали
TRANSCRIBE_TIMEOUT_SEC=3600 дважды (7184 с суммарно), абандоненный поток
2 часа удерживал MLX-локи, тост «Критическая ошибка» пришёл через 2 часа.
"""
from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import stt_budget  # noqa: E402


class BudgetFormulaTests(unittest.TestCase):
    """§4.2/§4.4: формула overhead + duration×factor с потолком профиля."""

    def test_incident_audio_interactive_budget_is_scaled_not_3600(self):
        # Спека-тест 1: 4.71 с → 90 + 4.71×3 = 104.13, НЕ 3600.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            got = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertAlmostEqual(got, 104.13, delta=0.5)
        self.assertLess(got, 3600.0)

    def test_batch_budget_is_larger_than_interactive_for_same_audio(self):
        # Спека-тест 2.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            inter = stt_budget.resolve_attempt_timeout_sec(4.71)
        with stt_budget.stt_budget_scope(stt_budget.BATCH):
            batch = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertGreater(batch, inter)

    def test_profile_cap_applies_for_52_minute_dictation(self):
        # Спека-тест 3: 52 мин = 3120 с → 90 + 3120×3 = 9450 → cap 1800.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            got = stt_budget.resolve_attempt_timeout_sec(3120.0)
        self.assertEqual(got, 1800.0)

    def test_unknown_duration_falls_back_to_profile_cap(self):
        # Спека-тест 4: fail-open в потолок ПРОФИЛЯ, не в час на interactive.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            self.assertEqual(stt_budget.resolve_attempt_timeout_sec(None), 1800.0)
        with stt_budget.stt_budget_scope(stt_budget.BATCH):
            self.assertEqual(stt_budget.resolve_attempt_timeout_sec(None), 3600.0)

    def test_no_scope_defaults_to_interactive(self):
        # §5: незалейбленный путь = interactive (fail-fast), не час.
        self.assertEqual(stt_budget.current_profile(), stt_budget.INTERACTIVE)
        self.assertEqual(stt_budget.resolve_attempt_timeout_sec(None), 1800.0)
        self.assertIsNone(stt_budget.remaining_sec())
        self.assertFalse(stt_budget.budget_exhausted())

    def test_explicit_deadline_clips_attempt_budget(self):
        # Спека-тест 5: REST deadline 30 с урезает расчётные 104 с.
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=30.0
        ):
            got = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertLessEqual(got, 30.0)
        self.assertGreaterEqual(got, stt_budget.MIN_USEFUL_ATTEMPT_SEC)

    def test_expired_deadline_floors_at_min_useful_and_reports_exhausted(self):
        # Спека-тесты 6 и 16: future.result никогда не получит отрицательный
        # таймаут — resolve floor'ится, а budget_exhausted говорит «не сабмить».
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=0.0
        ):
            self.assertTrue(stt_budget.budget_exhausted())
            got = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertEqual(got, stt_budget.MIN_USEFUL_ATTEMPT_SEC)

    def test_remaining_sec_decreases_monotonically(self):
        # Спека-тест 6.
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=60.0
        ):
            first = stt_budget.remaining_sec()
            time.sleep(0.05)
            second = stt_budget.remaining_sec()
        self.assertLess(second, first)

    def test_settings_snapshot_overrides_defaults(self):
        # Спека-тест 18 (ядро): значения берутся из снапшота на входе scope,
        # НЕ из engine._settings_get (в REST-процессе тот — заглушка).
        snap = {"stt_timeout_overhead_sec": 30.0,
                "stt_timeout_interactive_factor": 1.0}
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, settings_get=snap.get
        ):
            got = stt_budget.resolve_attempt_timeout_sec(10.0)
        self.assertAlmostEqual(got, 40.0, delta=0.01)

    def test_knob_garbage_is_clamped_or_defaulted(self):
        # Спека-тест 9 (модульная половина): NaN/мусор/1e9 не проходят.
        cases = {
            "stt_timeout_overhead_sec": float("nan"),
            "stt_timeout_interactive_factor": "мусор",
            "stt_timeout_interactive_max_sec": 10 ** 9,
        }
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, settings_get=cases.get
        ):
            got = stt_budget.resolve_attempt_timeout_sec(None)
        # max_sec заклампился к верхней границе 7200, не к 10**9.
        self.assertLessEqual(got, 7200.0)

    def test_timeout_blacklist_allowed_semantics(self):
        # §4.7: исчерпанный бюджет запроса → блэклист запрещён;
        # живой дедлайн / нет дедлайна → разрешён.
        self.assertTrue(stt_budget.timeout_blacklist_allowed())
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=600.0
        ):
            self.assertTrue(stt_budget.timeout_blacklist_allowed())
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=0.0
        ):
            self.assertFalse(stt_budget.timeout_blacklist_allowed())


class BudgetScopeTests(unittest.TestCase):
    """§4.1: изоляция тредов, сброс токена, пропагация через call_in_scope."""

    def test_thread_isolation(self):
        # Спека-тест 7: чужой тред не видит scope главного.
        seen: dict[str, object] = {}

        def _probe():
            seen["profile"] = stt_budget.current_profile()
            seen["remaining"] = stt_budget.remaining_sec()

        with stt_budget.stt_budget_scope(
            stt_budget.BATCH, deadline_sec=600.0
        ):
            t = threading.Thread(target=_probe)
            t.start()
            t.join(timeout=5)
        self.assertEqual(seen["profile"], stt_budget.INTERACTIVE)
        self.assertIsNone(seen["remaining"])

    def test_scope_resets_on_exception(self):
        # Спека-тест 8.
        with self.assertRaises(RuntimeError):
            with stt_budget.stt_budget_scope(stt_budget.BATCH):
                raise RuntimeError("boom")
        self.assertEqual(stt_budget.current_profile(), stt_budget.INTERACTIVE)

    def test_call_in_scope_propagates_into_pool_worker_thread(self):
        # Спека-тест 13: ContextVar не наследуется тредом пула — scope обязан
        # открываться ВНУТРИ submitted callable. Это runtime-тест, который
        # поймал бы scope, открытый во Flask-треде вокруг submit.
        seen: dict[str, object] = {}

        def _fake_transcribe(path, **kw):
            seen["profile"] = stt_budget.current_profile()
            seen["remaining"] = stt_budget.remaining_sec()
            return {"text": "ok", "path": path}

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(
                stt_budget.call_in_scope,
                _fake_transcribe,
                "/tmp/x.wav",
                profile=stt_budget.INTERACTIVE,
                deadline_sec=42.0,
                settings_snapshot=None,
                quality_profile="balanced",
            )
            result = fut.result(timeout=10)
        finally:
            pool.shutdown(wait=True)
        self.assertEqual(result["text"], "ok")
        self.assertEqual(seen["profile"], stt_budget.INTERACTIVE)
        self.assertIsNotNone(seen["remaining"])
        self.assertLessEqual(seen["remaining"], 42.0)
        self.assertGreater(seen["remaining"], 30.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают правильно**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py -v -p no:cacheprovider`
Expected: FAIL/ERROR c `ModuleNotFoundError: No module named 'core.stt_budget'` (feature missing — правильная причина RED).

- [ ] **Step 3: Написать `KrabEar/core/stt_budget.py`**

```python
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
```

- [ ] **Step 4: Прогнать тесты — GREEN**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py -v -p no:cacheprovider`
Expected: все PASS (14 тестов).

- [ ] **Step 5: Commit**

```bash
git add KrabEar/core/stt_budget.py KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py
git commit -m "feat(stt): core/stt_budget — per-request бюджеты STT (ContextVar, формула, дедлайн)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Настройки — `DEFAULT_SETTINGS` + `_RANGE_FIELDS`

**Files:**
- Modify: `KrabEar/core/config.py` (~:1228, рядом с `"stt_download_stall_timeout_sec"`)
- Modify: `KrabEar/backend/settings_validator.py` (`_RANGE_FIELDS`, :76+)
- Test: `KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py` (класс `BudgetSettingsWiringTests`)

**Interfaces:**
- Consumes: `stt_budget.KNOB_BOUNDS` (Task 1).
- Produces: 6 ключей в `DEFAULT_SETTINGS` и `_RANGE_FIELDS` — имена ровно как в `KNOB_BOUNDS`.

- [ ] **Step 1: RED-тест синхронизации**

Добавить в тест-файл:

```python
class BudgetSettingsWiringTests(unittest.TestCase):
    """§9: DEFAULT_SETTINGS + _RANGE_FIELDS (правило wave-34) синхронны
    с KNOB_BOUNDS — единственным источником границ в core."""

    def test_default_settings_carry_all_knobs(self):
        from core.config import DEFAULT_SETTINGS
        for key, (_lo, _hi, default) in stt_budget.KNOB_BOUNDS.items():
            self.assertIn(key, DEFAULT_SETTINGS, key)
            self.assertEqual(DEFAULT_SETTINGS[key], default, key)

    def test_range_fields_clamp_all_knobs_with_same_bounds(self):
        # _RANGE_FIELDS достраивается из KNOB_BOUNDS импортом (validator уже
        # импортирует core — см. SUPPORTED_GIGAAM_ASR_MODES, :19). Тест —
        # guard от удаления этой достройки, не от рассинхрона литералов.
        from backend.settings_validator import _RANGE_FIELDS
        for key, (lo, hi, default) in stt_budget.KNOB_BOUNDS.items():
            self.assertIn(key, _RANGE_FIELDS, key)
            v_lo, v_hi, v_default, v_coerce = _RANGE_FIELDS[key]
            self.assertEqual((v_lo, v_hi, v_default), (lo, hi, default), key)
            self.assertIs(v_coerce, float, key)
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py::BudgetSettingsWiringTests -v -p no:cacheprovider`
Expected: FAIL — `AssertionError: 'stt_timeout_overhead_sec' not found in ...`.

- [ ] **Step 3: Вставить блок в `DEFAULT_SETTINGS`**

В `KrabEar/core/config.py` сразу после строки `"stt_download_stall_timeout_sec": 300.0,` (:1228):

```python
    # Спека 2026-08-26 stt-timeout-budgets: раздельные бюджеты STT.
    # Границы клампов — core/stt_budget.py::KNOB_BOUNDS (тест сверяет).
    "stt_timeout_overhead_sec": 90.0,
    "stt_timeout_interactive_factor": 3.0,
    "stt_timeout_batch_factor": 6.0,
    "stt_timeout_interactive_max_sec": 1800.0,
    "stt_timeout_batch_max_sec": 3600.0,
    "stt_timeout_request_attempts": 4.0,
```

- [ ] **Step 4: Достроить `_RANGE_FIELDS` из `KNOB_BOUNDS`**

🔴 НЕ дублировать литералы: `settings_validator.py` уже импортирует из core
(`from core.gigaam_compat import SUPPORTED_GIGAAM_ASR_MODES`, :19) — прецедент есть,
дословная копия шести кортежей была бы рассинхроном, ждущим своего часа.

В `KrabEar/backend/settings_validator.py` — импорт рядом с существующим core-импортом (:19):

```python
from core.stt_budget import KNOB_BOUNDS as _STT_BUDGET_KNOB_BOUNDS
```

И сразу ПОСЛЕ закрывающей скобки словаря `_RANGE_FIELDS` (найти строку `}` , завершающую литерал; следующая значимая строка — определение `_ENUM_FIELDS` или подобное):

```python
# Спека 2026-08-26 stt-timeout-budgets (wave-34: клампить любой временной
# тюнинг). Границы живут в core/stt_budget.py::KNOB_BOUNDS — единственном
# источнике; здесь только проекция в формат валидатора (+ coerce=float).
_RANGE_FIELDS.update({
    _key: (_lo, _hi, _default, float)
    for _key, (_lo, _hi, _default) in _STT_BUDGET_KNOB_BOUNDS.items()
})
```

NaN/мусор/выход за границы обрабатывает существующий общий цикл `:305` (протестирован wave-19) — новых веток валидатора не нужно.

- [ ] **Step 5: Verify GREEN + смежный тест валидатора**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py KrabEar/tests/test_settings_validator.py KrabEar/tests/test_settings_validator_nan_w19.py -v -p no:cacheprovider`
Expected: все PASS.

- [ ] **Step 6: Commit**

```bash
git add KrabEar/core/config.py KrabEar/backend/settings_validator.py KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py
git commit -m "feat(stt): 6 настроек бюджетов STT в DEFAULT_SETTINGS + _RANGE_FIELDS (wave-34 клампы)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `engine.py` — три точки таймаута, гейт блэклиста, break'и, error-код

**Files:**
- Modify: `KrabEar/core/engine.py` (импорт ~:42; `_maybe_multipass_retry` :1837–1975; `_transcribe_with_fallback_impl` :2307–2600)
- Modify: `KrabEar/backend/error_codes.py` (новая запись после `stt.transcribe_failed`, :705)
- Modify: `KrabEar/tests/test_error_codes.py:274` и `KrabEar/tests/test_recording_owner_telemetry.py:479` — пин `len(ERROR_REGISTRY)` 69 → 70
- Test: тест-файл волны (классы `EngineBudgetContractTests`, `MultipassBudgetBehaviorTests`)

**Interfaces:**
- Consumes: `stt_budget.resolve_attempt_timeout_sec`, `budget_exhausted`, `timeout_blacklist_allowed`, `MIN_USEFUL_ATTEMPT_SEC`, `ADAPTER_MIN_BUDGET_SEC`, `current_profile`, `stt_budget_scope` (тесты).
- Produces: engine больше нигде не читает `settings.TRANSCRIBE_TIMEOUT_SEC` в STT-циклах; error-код `stt.budget_exhausted` в `ERROR_REGISTRY`.

- [ ] **Step 1: RED — AST-контракты и поведенческий тест multipass**

Добавить в тест-файл:

```python
import ast


def _engine_source() -> str:
    return (PROJECT_ROOT / "core" / "engine.py").read_text(encoding="utf-8")


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"функция {name} не найдена в engine.py")


def _attr_names(node: ast.AST) -> list[str]:
    return [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)]


class EngineBudgetContractTests(unittest.TestCase):
    """Спека-тесты 10/14/17 (AST, привязка к именам функций — не строкам)."""

    FUNCS = ("_maybe_multipass_retry", "_transcribe_with_fallback_impl")

    def test_no_direct_transcribe_timeout_sec_in_stt_loops(self):
        # Спека-тест 10 (приём PR #1953): сиблинги не разойдутся снова.
        tree = ast.parse(_engine_source())
        for fname in self.FUNCS:
            node = _function_node(tree, fname)
            self.assertNotIn(
                "TRANSCRIBE_TIMEOUT_SEC", _attr_names(node),
                f"{fname} читает settings.TRANSCRIBE_TIMEOUT_SEC напрямую — "
                "обязана идти через stt_budget.resolve_attempt_timeout_sec",
            )

    def test_budget_helpers_are_wired_into_both_loops(self):
        tree = ast.parse(_engine_source())
        for fname in self.FUNCS:
            attrs = _attr_names(_function_node(tree, fname))
            self.assertIn("resolve_attempt_timeout_sec", attrs, fname)
            self.assertIn("budget_exhausted", attrs, fname)
            # Гейт блэклиста — напрямую или через хелпер engine.
            self.assertTrue(
                "timeout_blacklist_allowed" in attrs
                or "_blacklist_allowed_for" in attrs,
                f"{fname} не гейтит запись в _unavailable_models (§4.7)",
            )

    def test_adapter_branch_applies_min_budget_floor(self):
        # Спека-тест 17 (§4.8): floor против осиротевшего GigaAM-subprocess.
        node = _function_node(
            ast.parse(_engine_source()), "_transcribe_with_fallback_impl"
        )
        self.assertIn("ADAPTER_MIN_BUDGET_SEC", _attr_names(node))

    def test_budget_exhausted_error_code_registered(self):
        from backend.error_codes import ERROR_REGISTRY
        self.assertIn("stt.budget_exhausted", ERROR_REGISTRY)
        self.assertEqual(
            ERROR_REGISTRY["stt.budget_exhausted"]["severity"], "error"
        )


class MultipassBudgetBehaviorTests(unittest.TestCase):
    """Спека-тесты 12/14 (поведение): фейковый каскад ретраев multipass."""

    def _make_engine(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.engine import AudioEngine

        eng = object.__new__(AudioEngine)
        eng.current_model = "balanced-model"
        eng._unavailable_models = {}
        # Детерминируем читателей result: без внутренностей segments-логики.
        eng._raw_confidence_from_result = (
            lambda r: float(r.get("confidence") or 0.0)
        )
        calls: list[str] = []

        def _fake_transcribe_model(audio, model, prompt, language=None):
            calls.append(model)
            return {"text": "retry-text", "confidence": 0.99}

        eng._transcribe_model = _fake_transcribe_model
        fake_settings = SimpleNamespace(
            STT_MIN_CONFIDENCE_THRESHOLD=0.9,
            STT_MAX_RETRIES=2,
            model_max_list=["big-a", "big-b"],
            NETWORK_MODE="offline_strict",
        )
        return eng, calls, fake_settings, patch

    def test_multipass_retries_when_budget_alive(self):
        eng, calls, fake_settings, patch = self._make_engine()
        first = {"text": "низко", "confidence": 0.1, "model_used": "gigaam"}
        with patch("core.engine.settings", fake_settings), patch(
            "core.engine.should_skip_second_mlx_checkpoint",
            return_value=False,
        ):
            with stt_budget.stt_budget_scope(
                stt_budget.INTERACTIVE, deadline_sec=600.0
            ):
                result = eng._maybe_multipass_retry(None, "", "ru", first)
        self.assertEqual(calls, ["big-a"])  # 0.99 >= 0.9 → break после первой
        self.assertEqual(result["text"], "retry-text")

    def test_multipass_skips_all_retries_when_deadline_exhausted(self):
        # Спека-тест 12: счётчик попыток = 0, следующая модель не пробуется.
        eng, calls, fake_settings, patch = self._make_engine()
        first = {"text": "низко", "confidence": 0.1, "model_used": "gigaam"}
        with patch("core.engine.settings", fake_settings), patch(
            "core.engine.should_skip_second_mlx_checkpoint",
            return_value=False,
        ):
            with stt_budget.stt_budget_scope(
                stt_budget.INTERACTIVE, deadline_sec=0.0
            ):
                result = eng._maybe_multipass_retry(None, "", "ru", first)
        self.assertEqual(calls, [])
        self.assertEqual(result["text"], "низко")
        # Спека-тест 14: прерывание по бюджету НЕ отравляет блэклист.
        self.assertEqual(eng._unavailable_models, {})
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest "KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py::EngineBudgetContractTests" "KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py::MultipassBudgetBehaviorTests" -v -p no:cacheprovider`
Expected: FAIL — AST-тесты (helpers не подключены), error-код отсутствует, `test_multipass_skips_all_retries...` (break-гейта нет → calls == ["big-a"]).

- [ ] **Step 3: Правки `engine.py` — 8 точечных замен**

**(a) Импорт** — после строки 42 (`from core.transcript_context import build_initial_prompt`):

```python
from core import stt_budget  # noqa: E402
```

**(b) `_maybe_multipass_retry` — длительность до цикла.** Найти строку `        retries_done = 0` (:1886) и вставить перед ней:

```python
        # Спека 2026-08-26: бюджет ретрая масштабируется от длительности.
        _mp_duration_sec: float | None = None
        if isinstance(audio_data, np.ndarray) and len(audio_data) > 0:
            _mp_duration_sec = len(audio_data) / 16000.0
```

**(c) `_maybe_multipass_retry` — break-гейт.** Заменить:

```python
        for candidate in retry_candidates:
            if retries_done >= max_retries:
                break
```

на:

```python
        for candidate in retry_candidates:
            if retries_done >= max_retries:
                break
            if stt_budget.budget_exhausted(stt_budget.MIN_USEFUL_ATTEMPT_SEC):
                logger.warning(
                    "[STT] multipass: бюджет запроса исчерпан — ретраи "
                    "прерваны перед %s", candidate["name"],
                )
                break
```

**(d) `_maybe_multipass_retry` — таймаут попытки.** Заменить:

```python
                        attempt_result = future.result(timeout=settings.TRANSCRIBE_TIMEOUT_SEC)
```

на:

```python
                        attempt_result = future.result(
                            timeout=stt_budget.resolve_attempt_timeout_sec(
                                _mp_duration_sec
                            )
                        )
```

**(e0) Хелпер гейта блэклиста.** Добавить метод в класс `AudioEngine` рядом с `_is_model_unavailable` (:674) — общий для multipass и adapter-ветки, чтобы условие не жило в двух дословных копиях:

```python
    def _blacklist_allowed_for(self, exc: BaseException) -> bool:
        """§4.7 (спека 2026-08-26): можно ли писать модель в
        _unavailable_models по этому исключению.

        Таймаут из-за исчерпанного бюджета ЗАПРОСА не доказывает нездоровье
        модели: попытке просто не досталось времени. Записать её в блэклист
        (TTL 300 с) — значит увести следующую диктовку сразу в Remote STT и
        выдать «Критическая ошибка» на здоровом стеке. Любое другое
        исключение (MLX watchdog, крах воркера, OOM) блэклист заслуживает.
        """
        if not isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
            return True
        return stt_budget.timeout_blacklist_allowed()
```

**(e) `_maybe_multipass_retry` — гейт блэклиста** (:1960-1964). Заменить:

```python
                attempts.append({
                    "model": model_label,
                    "confidence": 0.0,
                    "latency_ms": latency_ms,
                    "error": str(exc),
                })
                self._unavailable_models[model_label] = time.monotonic()
```

на:

```python
                attempts.append({
                    "model": model_label,
                    "confidence": 0.0,
                    "latency_ms": latency_ms,
                    "error": str(exc),
                })
                if self._blacklist_allowed_for(exc):
                    self._unavailable_models[model_label] = time.monotonic()
```

**(f) `_transcribe_with_fallback_impl` — длительность chain-аудио.** Найти (после ресемпл-блока):

```python
        candidates = [self.current_model]
        if self.quality_profile == "max":
            candidates = list(dict.fromkeys(settings.model_max_list))
```

и вставить ПЕРЕД этим блоком:

```python
        # Спека 2026-08-26: длительность считается по chain_audio_data (после
        # ресемпла) — chunked-путь подаёт сюда уже нарезанный кусок и потому
        # бесплатно получает бюджет на чанк, а не на весь файл.
        _chain_duration_sec: float | None = None
        if isinstance(chain_audio_data, np.ndarray):
            _sr_for_dur = (
                16000.0 if chain_sample_rate is None else float(chain_sample_rate)
            )
            if _sr_for_dur > 0 and len(chain_audio_data) > 0:
                _chain_duration_sec = len(chain_audio_data) / _sr_for_dur
        elif isinstance(chain_audio_data, (str, Path)) and os.path.exists(
            str(chain_audio_data)
        ):
            try:
                import soundfile as _sf_dur
                _chain_duration_sec = float(
                    _sf_dur.info(str(chain_audio_data)).duration
                )
            except Exception:
                _chain_duration_sec = None
```

**(g) каскад — break-гейт.** Заменить:

```python
        for model_name in candidates:
            # Adapter ветки (не whisper).
            if model_name in _adapter_map:
```

на:

```python
        for model_name in candidates:
            if stt_budget.budget_exhausted(stt_budget.MIN_USEFUL_ATTEMPT_SEC):
                logger.warning(
                    "STT: бюджет запроса исчерпан — каскад прерван перед %s",
                    model_name,
                )
                self._push_error(
                    "stt.budget_exhausted",
                    f"budget exhausted before {model_name} "
                    f"(duration={_chain_duration_sec}, "
                    f"profile={stt_budget.current_profile()})",
                    severity="error",
                )
                break
            # Adapter ветки (не whisper).
            if model_name in _adapter_map:
```

**(h) adapter-таймаут** (:2503). Заменить:

```python
                    _adapter_timeout = getattr(settings, "TRANSCRIBE_TIMEOUT_SEC", 120)
```

на:

```python
                    # §4.8: floor поверх бюджета — внешний таймаут не смеет
                    # быть короче внутренних таймаутов GigaAM-subprocess
                    # (120s shortform / 180s load), иначе брошенный
                    # subprocess осиротеет с моделью на GPU.
                    _adapter_timeout = max(
                        stt_budget.resolve_attempt_timeout_sec(_chain_duration_sec),
                        stt_budget.ADAPTER_MIN_BUDGET_SEC,
                    )
```

**(i) adapter — гейт блэклиста** (:2548-2552). Заменить:

```python
                except Exception as exc:
                    logger.warning("%s adapter не сработал: %s — продолжаю chain", span_pfx, exc)
                    self._unavailable_models[model_name] = time.monotonic()
                    continue
```

на:

```python
                except Exception as exc:
                    logger.warning("%s adapter не сработал: %s — продолжаю chain", span_pfx, exc)
                    if self._blacklist_allowed_for(exc):
                        self._unavailable_models[model_name] = time.monotonic()
                    continue
```

**(j) whisper-ветка — таймаут** (:2568). Заменить:

```python
            try:
                timeout = settings.TRANSCRIBE_TIMEOUT_SEC
```

на:

```python
            try:
                timeout = stt_budget.resolve_attempt_timeout_sec(_chain_duration_sec)
```

**(k) whisper-ветка — лог + гейт блэклиста** (:2590-2596). Заменить:

```python
            except concurrent.futures.TimeoutError:
                logger.error(
                    "Таймаут %ds при транскрибации моделью %s — пропускаю",
                    settings.TRANSCRIBE_TIMEOUT_SEC, model_name,
                )
                self._unavailable_models[model_name] = time.monotonic()
```

на:

```python
            except concurrent.futures.TimeoutError:
                # Лог обязан называть СРАБОТАВШЕЕ число, не глобальную
                # константу — иначе следующий разбор идёт по ложному следу.
                logger.error(
                    "Таймаут %.0fs (профиль %s) при транскрибации моделью %s — пропускаю",
                    timeout, stt_budget.current_profile(), model_name,
                )
                if stt_budget.timeout_blacklist_allowed():
                    self._unavailable_models[model_name] = time.monotonic()
```

- [ ] **Step 4: Запись в `ERROR_REGISTRY`**

В `KrabEar/backend/error_codes.py` после закрывающей скобки записи `"stt.transcribe_failed"` (:705):

```python
    # stt.budget_exhausted — спека 2026-08-26 stt-timeout-budgets: каскад STT
    # прерван по исчерпанию бюджета ЗАПРОСА (профиль interactive/batch,
    # масштабируется от длительности аудио). Отличает «запросу не хватило
    # времени» от «модель нездорова» (stt.mlx_timeout / stt.load_fail);
    # модель при этом коде в _unavailable_models НЕ попадает.
    "stt.budget_exhausted": {
        "user_msg_ru": "STT: распознавание не уложилось в бюджет времени",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 30,
    },
```

Обновить пины количества: в `KrabEar/tests/test_error_codes.py:274` и `KrabEar/tests/test_recording_owner_telemetry.py:479` заменить `self.assertEqual(len(ERROR_REGISTRY), 69)` на `self.assertEqual(len(ERROR_REGISTRY), 70)`.

- [ ] **Step 5: Verify GREEN + зависящие тесты STT**

Run:
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py KrabEar/tests/test_error_codes.py KrabEar/tests/test_recording_owner_telemetry.py KrabEar/tests/test_stt_remote_sibling_gate_2026_08_26.py -v -p no:cacheprovider
```
Expected: все PASS. (`test_stt_remote_sibling_gate` — AST-тест PR #1953 по тем же функциям: обязан остаться зелёным.)

- [ ] **Step 6: Commit**

```bash
git add KrabEar/core/engine.py KrabEar/backend/error_codes.py KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py KrabEar/tests/test_error_codes.py KrabEar/tests/test_recording_owner_telemetry.py
git commit -m "feat(stt): engine на stt_budget — масштабируемые таймауты, break по дедлайну, блэклист-гейт §4.7

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `recording_core_service` — scope по owner + batch-импорт

**Files:**
- Modify: `KrabEar/backend/recording_core_service.py` (`_run_stop_recording_tail` ~:1589; `_transcribe_paths_core` ~:3688)
- Test: тест-файл волны (класс `ScopeWiringOwnerTests`)

**Interfaces:**
- Consumes: `stt_budget.stt_budget_scope`, `stt_budget.BATCH`, `stt_budget.INTERACTIVE`.
- Produces: модульная функция `stt_budget_profile_for_owner(owner: str | None) -> str` в `recording_core_service.py` (уровень модуля, тестируется без создания сервиса).

- [ ] **Step 1: RED-тесты**

```python
class ScopeWiringOwnerTests(unittest.TestCase):
    """Спека-тесты 11 (частично) и 15: профиль по владельцу поколения (R2)."""

    def test_owner_profile_mapping(self):
        from backend.recording_core_service import stt_budget_profile_for_owner
        self.assertEqual(stt_budget_profile_for_owner("meeting"), "batch")
        self.assertEqual(stt_budget_profile_for_owner("dictation"), "interactive")
        self.assertEqual(stt_budget_profile_for_owner("quick_capture"), "interactive")
        self.assertEqual(stt_budget_profile_for_owner(None), "interactive")
        self.assertEqual(stt_budget_profile_for_owner(""), "interactive")

    def test_stop_tail_and_batch_import_open_budget_scope(self):
        # AST-контракт §10.11. Используется строгий помощник из Task 5
        # (`assert_stt_budget_scope_wraps_transcribe`), но с обобщением:
        # 🔴 в `_run_stop_recording_tail` внутри scope стоит НЕ `transcribe`,
        # а `_stop_recording_phase_c` (сам transcribe живёт внутри фазы), и
        # профиль там — ВЫЧИСЛЯЕМАЯ переменная, а не литерал. Поэтому помощник
        # надо расширить двумя необязательными параметрами:
        #   inner_call_attr: str = "transcribe"   — что искать внутри тела with
        #   expected_profile: str | None          — None = профиль не проверять
        # Правку сделай в самом помощнике, существующие вызовы Task 5 не ломая
        # (их поведение при дефолтах обязано остаться прежним).
        assert_stt_budget_scope_wraps_transcribe(
            "backend/recording_core_service.py",
            "_run_stop_recording_tail",
            expected_profile=None,
            inner_call_attr="_stop_recording_phase_c",
        )
        assert_stt_budget_scope_wraps_transcribe(
            "backend/recording_core_service.py",
            "_transcribe_paths_core",
            expected_profile="batch",
        )
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest "KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py::ScopeWiringOwnerTests" -v -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'stt_budget_profile_for_owner'`.

- [ ] **Step 3: Правки `recording_core_service.py`**

**(a) Импорт** — в блок импортов вверху файла:

```python
from core import stt_budget
```

**(b) Модульная функция** — на уровне модуля, перед классом `RecordingCoreService`:

```python
def stt_budget_profile_for_owner(owner: str | None) -> str:
    """Профиль STT-бюджета по владельцу поколения записи (R2, спека
    2026-08-26 §5). meeting — многочасовые записи, живой человек результата
    в момент стопа не ждёт (панель уже показала транскрипт по ходу) → batch.
    dictation/quick_capture/неизвестный owner → interactive (fail-fast)."""
    return stt_budget.BATCH if owner == "meeting" else stt_budget.INTERACTIVE
```

**(c) `_run_stop_recording_tail`** — заменить:

```python
        # Phase C: STT execution
        phase_c = self._stop_recording_phase_c(audio, duration_sec, sr)
```

на:

```python
        # Phase C: STT execution — под бюджет-scope (спека 2026-08-26):
        # профиль по владельцу поколения, дедлайн от длительности записи.
        # Тред тот же (phase_c синхронна) — ContextVar виден engine.
        _budget_profile = stt_budget_profile_for_owner(
            (phase_a.get("generation") or {}).get("owner")
        )
        with stt_budget.stt_budget_scope(
            _budget_profile,
            settings_get=lambda k, d: settings.get(k, d),
            audio_duration_sec=duration_sec,
        ):
            phase_c = self._stop_recording_phase_c(audio, duration_sec, sr)
```

**(d) `_transcribe_paths_core`** — заменить блок (после вычисления `audio_duration_sec` и `import_lang_hint`):

```python
                bump_stt_activity()
                if progress_callback is not None:
                    self.transcriber.engine.set_quality_profile(quality_profile)
                    transcribe_payload = self.transcriber.engine.transcribe(
                        audio_path,
                        cleanup_profile=cleanup_profile,
                        is_preview=False,
                        domain="casual",
                        extra_vocabulary=user_vocabulary if user_vocabulary else None,
                        lang_hint=import_lang_hint,
                        progress_callback=progress_callback,
                    )
                else:
                    transcribe_payload = self.transcriber.transcribe(
                        audio_path,
                        quality_profile=quality_profile,
                        cleanup_profile=cleanup_profile,
                        lang_hint=import_lang_hint,
                        extra_vocabulary=user_vocabulary if user_vocabulary else None,
                    )
```

на:

```python
                bump_stt_activity()
                # Спека 2026-08-26 §5: пакетный импорт → batch-бюджет,
                # per-file (audio_duration_sec уже вычислен выше).
                with stt_budget.stt_budget_scope(
                    stt_budget.BATCH,
                    settings_get=lambda k, d: settings.get(k, d),
                    audio_duration_sec=audio_duration_sec,
                ):
                    if progress_callback is not None:
                        self.transcriber.engine.set_quality_profile(quality_profile)
                        transcribe_payload = self.transcriber.engine.transcribe(
                            audio_path,
                            cleanup_profile=cleanup_profile,
                            is_preview=False,
                            domain="casual",
                            extra_vocabulary=user_vocabulary if user_vocabulary else None,
                            lang_hint=import_lang_hint,
                            progress_callback=progress_callback,
                        )
                    else:
                        transcribe_payload = self.transcriber.transcribe(
                            audio_path,
                            quality_profile=quality_profile,
                            cleanup_profile=cleanup_profile,
                            lang_hint=import_lang_hint,
                            extra_vocabulary=user_vocabulary if user_vocabulary else None,
                        )
```

`settings` в `_transcribe_paths_core` уже загружены (`settings = self._settings_svc.cached_settings()`, :3642).

- [ ] **Step 4: Verify GREEN + смежные recording-тесты**

Run:
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py -v -p no:cacheprovider
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_owner_telemetry.py KrabEar/tests/test_backend_service.py -x -q -p no:cacheprovider
```
Expected: PASS. (`test_backend_service` — smoke, что stop-путь не сломан. Упал — прогнать тот же файл на `git stash`-нетронутом коде: `git stash push -u -m stt-budget-t4 && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_backend_service.py -x -q -p no:cacheprovider; git stash list --format='%H %gs' | head -1` → восстановить `git stash apply <sha>` и drop по маркеру. Красный и там — падение предсуществующее, задачу не блокирует; красный только с правками — дефект правок, чинить до коммита.)

- [ ] **Step 5: Commit**

```bash
git add KrabEar/backend/recording_core_service.py KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py
git commit -m "feat(stt): budget-scope в stop-финализации (профиль по owner R2) и batch-импорте

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `bulk_reprocess` + `live_subs_service` — оставшиеся точки §5

**Files:**
- Modify: `KrabEar/backend/bulk_reprocess.py` (`_run_locked`, вызов transcribe :412)
- Modify: `KrabEar/backend/live_subs_service.py` (`_process_window`, вызов transcribe :633)
- Test: тест-файл волны (класс `ScopeWiringRemainingPathsTests`)

**Interfaces:**
- Consumes: `stt_budget.stt_budget_scope`, `BATCH`, `INTERACTIVE`.

- [ ] **Step 1: RED — AST-контракт (завершение спека-теста 11)**

```python
class ScopeWiringRemainingPathsTests(unittest.TestCase):
    """§10.11: каждая точка §5 обёрнута в scope — bulk_reprocess, live_subs."""

    def _assert_scope_in(self, rel_path: str, func_name: str, profile: str) -> None:
        # 🔴 Слабая проверка «имя stt_budget_scope есть где-то в теле» прошла бы
        # и при scope, открытом НЕ вокруг transcribe. Контракт обязан проверять
        # три вещи разом: (1) есть with со scope, (2) внутри его тела — вызов
        # transcribe, (3) первый аргумент scope — ожидаемый профиль.
        assert_scope_wraps_transcribe(PROJECT_ROOT / rel_path, func_name, profile)

    def test_bulk_reprocess_opens_batch_scope(self):
        self._assert_scope_in("backend/bulk_reprocess.py", "_run_locked", "BATCH")

    def test_live_subs_opens_interactive_scope(self):
        self._assert_scope_in(
            "backend/live_subs_service.py", "_process_window", "INTERACTIVE"
        )
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest "KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py::ScopeWiringRemainingPathsTests" -v -p no:cacheprovider`
Expected: FAIL оба.

- [ ] **Step 3: Правка `bulk_reprocess.py`**

Импорт вверху файла: `from core import stt_budget`.

В `_run_locked` заменить:

```python
                from core.mlx_lock import mlx_lock
                from core.mlx_inter_lock import mlx_inter_process_lock
                with mlx_inter_process_lock(), mlx_lock():  # W1635: cross-process flock + intra-process RLock
                    result = self.transcriber.transcribe(
                        audio_data,
                        quality_profile="balanced",
                        cleanup_profile="soft",
                        lang_hint=item.source_lang or None,
                    )
```

на:

```python
                from core.mlx_lock import mlx_lock
                from core.mlx_inter_lock import mlx_inter_process_lock
                import numpy as _np_budget
                # Спека 2026-08-26 §5: bulk-reprocess → batch-бюджет.
                # settings_get=None → дефолты модуля (у reprocessor'а нет
                # settings-коллаборатора; batch-дефолты достаточны).
                _dur_sec = (
                    len(audio_data) / 16000.0
                    if isinstance(audio_data, _np_budget.ndarray)
                    and len(audio_data) > 0
                    else None
                )
                # 🔴 Порядок вложенности: локи СНАРУЖИ, бюджет ВНУТРИ.
                # Бюджет = overhead + длительность×factor — это модель РАБОТЫ
                # (загрузка модели + инференс), а не очереди. mlx_lock() —
                # RLock БЕЗ таймаута: под контенцией (финализация многочасовой
                # встречи держит GPU) ожидание тянется минутами, и короткий
                # item исчерпал бы дедлайн НЕ НАЧАВ распознавание — потеря
                # записи, регрессия против прежних 3600с. Инцидент волны был
                # про УДЕРЖАНИЕ ресурса, не про ожидание его.
                with mlx_inter_process_lock(), mlx_lock():  # W1635: cross-process flock + intra-process RLock
                    with stt_budget.stt_budget_scope(
                        stt_budget.BATCH, audio_duration_sec=_dur_sec
                    ):
                        result = self.transcriber.transcribe(
                            audio_data,
                            quality_profile="balanced",
                            cleanup_profile="soft",
                            lang_hint=item.source_lang or None,
                        )
```

- [ ] **Step 4: Правка `live_subs_service.py`**

Импорт вверху файла: `from core import stt_budget` (numpy уже импортирован как `np`, :26).

В `_process_window` заменить:

```python
        try:
            stt_result = self._transcriber.transcribe(
                audio, quality_profile="balanced", skip_vad_prefilter=True,
                context_free=True, lang_hint=lang_hint, single_pass=True,
            )
```

на:

```python
        try:
            # Спека 2026-08-26 §5: live subs — interactive (окно ~3 с; scope
            # в этом же worker-треде сервиса, ContextVar виден engine).
            with stt_budget.stt_budget_scope(
                stt_budget.INTERACTIVE,
                settings_get=self._settings_get,
                audio_duration_sec=(
                    len(audio) / 16000.0
                    if isinstance(audio, np.ndarray) and len(audio) > 0
                    else None
                ),
                # quiet: окно приходит ~раз в 3 с (~1200 строк/час) — INFO
                # на каждое окно утопил бы строки диктовки и импорта.
                quiet=True,
            ):
                stt_result = self._transcriber.transcribe(
                    audio, quality_profile="balanced", skip_vad_prefilter=True,
                    context_free=True, lang_hint=lang_hint, single_pass=True,
                )
```

(`finally:`-блок с `self._stt_release()` остаётся снаружи без изменений.)

- [ ] **Step 5: Verify GREEN + смежные live_subs тесты**

Run:
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py -v -p no:cacheprovider
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_bulk_reprocess.py KrabEar/tests/test_bulk_reprocess_recording_guard_W1043.py KrabEar/tests/test_live_subs_hardening_W1770.py KrabEar/tests/test_live_subs_language_routing_2026_08_12.py -q -p no:cacheprovider
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add KrabEar/backend/bulk_reprocess.py KrabEar/backend/live_subs_service.py KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py
git commit -m "feat(stt): budget-scope в bulk-reprocess (batch) и live subs (interactive)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: REST — `call_in_scope` в submit + связка `deadline_sec`

**Files:**
- Modify: `KrabEar/backend/rest_server.py` (импорт; submit-блок :1778-1782)
- Test: тест-файл волны (класс `RestScopeWiringTests` — AST без импорта rest_server)

**Interfaces:**
- Consumes: `stt_budget.call_in_scope` (Task 1; runtime-пропагация уже покрыта `test_call_in_scope_propagates_into_pool_worker_thread`).

- [ ] **Step 1: RED — AST-контракт**

🔴 Не импортировать `backend.rest_server` (module-level `AudioEngine`/`StateStore` — chunk-pollution класс). Только AST файла:

```python
class RestScopeWiringTests(unittest.TestCase):
    """§4.1/§6: REST сабмитит transcribe ЧЕРЕЗ call_in_scope — scope
    открывается в worker-треде пула, deadline_sec связан с бюджетом."""

    def test_rest_submits_transcribe_through_call_in_scope(self):
        src = (PROJECT_ROOT / "backend" / "rest_server.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        found_scoped_submit = False
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit"
            ):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if (
                isinstance(first, ast.Attribute)
                and first.attr == "call_in_scope"
            ):
                found_scoped_submit = True
            # Голый submit(deps.transcriber.transcribe, ...) запрещён:
            # ContextVar не наследуется worker-тредом (§4.1).
            self.assertFalse(
                isinstance(first, ast.Attribute)
                and first.attr == "transcribe",
                "rest_server сабмитит transcribe напрямую — scope не "
                "доедет до worker-треда",
            )
        self.assertTrue(found_scoped_submit)
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest "KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py::RestScopeWiringTests" -v -p no:cacheprovider`
Expected: FAIL (`found_scoped_submit` False + прямой submit найден).

- [ ] **Step 3: Правка `rest_server.py`**

Импорт вверху (рядом с другими `core.*`): `from core import stt_budget`.

Заменить submit-блок:

```python
        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _pool_shutdown_nonblocking = False
        try:
            _future = _pool.submit(deps.transcriber.transcribe, _transcribe_path, **_transcribe_kwargs)
```

на:

```python
        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _pool_shutdown_nonblocking = False
        # Спека 2026-08-26 §4.1/§6: scope открывается ВНУТРИ worker-треда
        # (call_in_scope) — ContextVar не наследуется тредом пула. Снапшот
        # настроек берётся здесь: engine в REST-процессе создан без
        # settings_get и сам настройки прочитать не может (§4.2).
        try:
            _budget_settings_snapshot = deps.store.load_settings(nowait=True)
        except Exception:
            _budget_settings_snapshot = None
        try:
            _future = _pool.submit(
                stt_budget.call_in_scope,
                deps.transcriber.transcribe,
                _transcribe_path,
                profile=stt_budget.INTERACTIVE,
                deadline_sec=_transcribe_timeout_sec,
                settings_snapshot=_budget_settings_snapshot,
                **_transcribe_kwargs,
            )
```

(Внешний `_future.result(timeout=_transcribe_timeout_sec)` и 504-ветка не меняются — внешний контур остаётся страховкой, внутренний теперь вложен корректно: `attempt ≤ request deadline`.)

- [ ] **Step 4: Verify GREEN + REST-смежные**

Run:
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py -v -p no:cacheprovider
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_e2e.py -q -p no:cacheprovider
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KrabEar/backend/rest_server.py KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py
git commit -m "feat(stt): REST submit через call_in_scope — deadline_sec связан с бюджетами (W2c)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Гейты волны + NOW.md

**Files:**
- Modify: `docs/NOW.md` (строка про root-cause)

- [ ] **Step 1: flake8 CI-командой**

Run (та же команда, что CI, по изменённым файлам):
```bash
source .venv_krab_ear/bin/activate && flake8 KrabEar/core/stt_budget.py KrabEar/core/engine.py KrabEar/core/config.py KrabEar/backend/settings_validator.py KrabEar/backend/recording_core_service.py KrabEar/backend/bulk_reprocess.py KrabEar/backend/live_subs_service.py KrabEar/backend/rest_server.py KrabEar/backend/error_codes.py KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py \
  --max-line-length=150 --extend-ignore=E501 \
  --per-file-ignores='KrabEar/tests/*:F401,F541,F841,E203,E301,E302,E303,E305,E306,E401,E402,W391' \
  --statistics
```
Expected: пусто (0 ошибок). Команда — зеркало гейта `krabear-ci.yml:61` (🔴 W293 в тестах НЕ в списке ignores — пробельные строки с отступами в тест-файле красят CI).

- [ ] **Step 2: ubuntu-parity (обязателен — STT-ветки без mlx)**

Run: `make pre-merge-check` (или `scripts/pre_merge_py312_check.sh KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py`)
Expected: ALL GREEN.

- [ ] **Step 3: audit-all (новый core-модуль обязан иметь прод-импортёра)**

Run: `make audit-all`
Expected: CLEAN — `stt_budget` импортируют engine/recording_core_service/bulk_reprocess/live_subs_service/rest_server, dead-module guard не сработает.

- [ ] **Step 4: полный прогон затронутых + dispatch-инварианты**

Run:
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_timeout_budgets_2026_08_26.py KrabEar/tests/test_error_codes.py KrabEar/tests/test_recording_owner_telemetry.py KrabEar/tests/test_stt_remote_sibling_gate_2026_08_26.py KrabEar/tests/test_dispatch_error_contract.py -v -p no:cacheprovider
make dispatch-tests
```
Expected: все PASS.

- [ ] **Step 5: строка в `docs/NOW.md`**

Добавить в раздел открытых пунктов:

```markdown
- 🔴 Root-cause инцидента 26.08 НЕ закрыт волной бюджетов: `_transcribe_model`
  висел час МИМО 45-с watchdog'а — подозреваемый `mlx_lock()` без таймаута
  (`engine.py:2762`, тот же класс, что превью-инцидент 13.08). Отдельная волна.
```

- [ ] **Step 6: Commit + push**

```bash
git add docs/NOW.md
git commit -m "docs(NOW): root-cause часового зависания mlx_lock — отдельная волна

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin feat/stt-timeout-budgets
```

---

## После задач (фаза ревью — вне этого плана, но обязательна)

1. **Fable-ревью ЦЕЛОГО диффа ветки** (не по коммитам) — паттерн, ловивший HIGH в C3a/C3b/S34.
2. **Живой e2e**: `scripts/run_e2e_smokes.command` (throwaway backend, никогда прод).
3. PR в `codex/krab-ear-v2`; merge только при зелёном CI (`gh pr checks` по полному headSha).
4. Прод не рестартовать в обход `scripts/safe_backend_restart.command`.

## Self-review плана (выполнен)

- Покрытие спеки: §4.1→T1/T6, §4.2→T1, §4.3→T3(f), §4.4→T1-тесты, §4.6→T1, §4.7→T3(e,i,k)+тест 14, §4.8→T3(h)+тест 17, §5→T4/T5/T6, §6→T6, §7→T3(c,g)+error-код, §9→T2, §10.1-18→T1-T6, §11→T7. Спека-тест 9 закрыт парой: клампы `_read_knob` (T1) + структурная сверка `_RANGE_FIELDS` (T2) — NaN-поведение самого валидатора уже пинит wave-19-тест.
- Типы/имена сквозные: `stt_budget_scope`, `call_in_scope`, `resolve_attempt_timeout_sec`, `timeout_blacklist_allowed`, `stt_budget_profile_for_owner` — единообразны во всех задачах.
- Два отклонения от буквы спеки зафиксированы в Global Constraints (BudgetExhaustedTimeout; сигнатура resolve).
