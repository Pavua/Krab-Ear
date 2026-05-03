# Phase B — Loud Errors (detailed design)

**Status:** approved scope, design phase complete, awaiting user review.
**Branch target:** `codex/krab-ear-v2`
**Author:** Pavel + Claude (Opus 4.7)
**Date:** 2026-05-04
**Parent spec:** `2026-05-02-stability-roadmap-design.md` (Phase B section, lines 114-252)
**Prerequisite shipped:** Phase A — Auto-heal (BackendSupervisor + HealthMonitor + BackendToast + StatusIndicator), 2026-05-02
**Baseline in flight:** PR [#362](https://github.com/Pavua/Krab-Ear/pull/362) — rewriter resilience (timeout 45s, JIT retry, structured `logger.warning`, `warmup()`)

---

## Context

### Why now

Phase A closed «процесс падает молча» — supervisor поднимает backend, dot в menu bar показывает state процесса. Observation period (2 дня) выявил параллельную слепую зону: **сам процесс жив, но конкретный downstream сервис (LLM rewriter, AX paste, diarization, …) тихо отказал**, и пользователь узнаёт об этом только по деградации качества вывода.

Конкретный инцидент 2026-05-04:
- LM Studio idle-evicted gemma-4-e4b-it-mlx
- Cold-load занял ~11 s, превысив старый 20 s timeout под нагрузкой
- 3 timeout'а подряд → CircuitBreaker в `llm_rewriter.py` ушёл в `OPEN`
- Exp backoff: 60 → 120 → 240 → 480 → 600 s
- Circuit практически постоянно `OPEN` несколько часов
- Все три failure-ветки (`requests.Timeout`, `requests.ConnectionError`, HTTP non-200) **не писали в лог** — `_last_error` обновлялся, но `logger` молчал
- Phase A `HealthMonitor` пингал только Python IPC (socket alive) — не HTTP rewriter
- Симптом: пользователь надиктовал три сообщения с заметным падением качества (raw STT artefacts вроде «0з» / «1временно»), посчитал что «модель не загружается»

Это типовая форма для **девяти** silent-failure классов в кодовой базе. Phase B их перечисляет, делает observable, и для большинства добавляет actionable восстановление.

### Today's gap not in parent spec

Parent spec (lines 114-252) не покрывал **active LLM HTTP probe**. `HealthMonitor` в Phase A тестирует только `ping` IPC handler — это не ловит ситуацию «backend здоров, LM Studio мёртв». Phase B добавляет вторичный канал в `HealthMonitor` который активно проверяет каждый critical downstream (LLM, диск для history, опционально HF token), а не только в момент пользовательского запроса.

### Relationship to PR #362

PR #362 — это **B.0 baseline**: добавил `logger.warning(...)` в три ранее silent failure-ветки `LLMRewriter`. Это позволяет Phase B.1 видеть события в логе и параллельно подключить `error_bus.push(…)` рядом с `logger.warning` без ломки старого пути. PR #362 — **prerequisite, не часть Phase B.** После B.1 merge старый `logger.warning` остаётся (для grep'а в логе и Sentry breadcrumb context), но parallel `error_bus.push` становится authoritative источником для UI.

---

## Goals

1. **Каждый из 9 silent-failure классов виден пользователю** — toast / status badge / Diagnostics tab.
2. **Actionable восстановление** где разумно — кнопка «Open Privacy Settings», «Указать HF token», «Disable rewriter», «Open hotkey settings».
3. **Active probe critical downstream** в `HealthMonitor` — не только IPC, но и LLM HTTP, history-write feasibility.
4. **Структурированный error log** — `KrabError` Pydantic model, единый registry, JSON-friendly для Sentry/GlitchTip.
5. **Severity-tier Sentry routing** — info=local, warn=batched, error/critical=immediate.
6. **Diagnostics tab** — last 200 errors с фильтрами, copy-to-clipboard, open-log-file.
7. **Dedupe** — toast spam при повторяющихся ошибках предотвращён 30 s window per `code`.

## Non-goals

- VA Phase 2/3 (Live Translation / Call Automation) — отдельные spec'и.
- STT adapter Phase 4 — отдельный roadmap (`project_research_backlog_2026-04.md`).
- Phase C root-cause (memory leaks, MLX lock audit, IPC reconnect) — parent spec, отдельный roadmap.
- Telegram-driven `kill_lm_studio_via_telegram` action — depends on main Krab bot, защищено feature flag, реальная проводка отложена.
- Auto-fix без подтверждения пользователя — Phase B всегда показывает кнопку, не дёргает действие.

---

## Architecture

### Layer 1 — Structured Error Bus (Python)

Новый `KrabEar/backend/error_bus.py`. Обёртка над существующим `event_bus.py` (`backend/event_bus.py`), не замена.

```python
import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

logger = logging.getLogger("KrabEar.Backend.ErrorBus")

Severity = Literal["info", "warn", "error", "critical"]

_SEVERITY_TO_LOG_LEVEL = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

class KrabError(BaseModel):
    severity: Severity
    component: Literal[
        "stt", "rewriter", "paste", "diarization",
        "translation", "mlx", "history", "vocabulary", "hotkey"
    ]
    code: str                   # registry key from error_codes.py
    message_user: str           # ru, для UI; обычно из registry.user_msg_ru
    message_debug: str          # для логa, может содержать exception repr
    timestamp: datetime
    context: dict               # arbitrary {"model": "...", "duration_ms": 15000}
    actionable: bool            # есть ли button "Fix" в UI
    action_id: Optional[str]    # ID для handle_error_action IPC


class ErrorBus:
    def __init__(
        self,
        event_bus,
        registry: dict,                # ERROR_REGISTRY from error_codes.py
        sentry_client=None,
        default_dedupe_window_sec: float = 30.0,
        ring_buffer_size: int = 200,
    ):
        self._event_bus = event_bus
        self._registry = registry
        self._sentry = sentry_client
        self._default_dedupe = default_dedupe_window_sec
        self._dedupe: dict[str, float] = {}            # code → last_emit monotonic
        self._lock = threading.Lock()
        self._ring: deque = deque(maxlen=ring_buffer_size)

    def push(self, err: KrabError) -> bool:
        """Returns True if emitted, False if deduped."""
        with self._lock:
            now = time.monotonic()
            last = self._dedupe.get(err.code)
            if last is not None and (now - last) < self._dedupe_window_for(err.code):
                return False
            self._dedupe[err.code] = now
            self._ring.append(err)

        logger.log(
            _SEVERITY_TO_LOG_LEVEL.get(err.severity, logging.INFO),
            "krab_error code=%s severity=%s component=%s msg=%s ctx=%s",
            err.code, err.severity, err.component, err.message_debug, err.context,
        )
        self._event_bus.emit("krab_error", err.model_dump(mode="json"))
        self._route_to_sentry(err)
        return True

    def _dedupe_window_for(self, code: str) -> float:
        return float(self._registry.get(code, {}).get(
            "dedupe_seconds", self._default_dedupe
        ))

    def _route_to_sentry(self, err: KrabError) -> None:
        # Tier mapping: info=skip, warn=batch (10/30s), error/critical=immediate.
        # Implementation detail: WarnBatcher helper class flushes in 30s windows.
        if self._sentry is None or err.severity == "info":
            return
        if err.severity == "warn":
            self._sentry_warn_batch(err)
        else:
            self._sentry.capture_message(
                err.message_debug,
                level=err.severity,
                tags={"phase": "b", "code": err.code, "component": err.component},
                extras=err.context,
            )

    def list_recent(self, limit: int = 200) -> list[KrabError]:
        with self._lock:
            return list(self._ring)[-limit:]

    def clear(self) -> int:
        with self._lock:
            n = len(self._ring)
            self._ring.clear()
            return n
```

**Dedupe semantics:** per-code window. Default 30 s. Override через `error_codes.py` registry — например `paste.ax_denied` имеет 60 s (пользователь надиктует много фраз подряд при отозванной AX, не нужно тостовать каждую), `mlx.oom` 5 s (всё горит, нужно видеть быстро).

**Thread safety:** `error_bus.push()` может вызываться из любого треда (transcription pipeline, MLX worker subprocess, IPC handler). Single `threading.Lock` достаточен — push'ы редкие, не hot path.

### Layer 2 — IPC contract

**Existing event bus** уже стримит SSE-like events в Swift через `/events` endpoint (`backend/event_bus.py`). Phase B reuse'ит этот канал, добавляя новый event type `"krab_error"`.

Новые IPC методы:

```jsonc
// Свежий снимок последних N errors (для Diagnostics tab cold-load)
{"id": "...", "method": "list_recent_errors", "params": {"limit": 200}}
// →
{"id": "...", "ok": true, "result": {"errors": [<KrabError JSON>, ...]}}

// Action handler (нажатие кнопки "Fix" в toast или Diagnostics tab)
{"id": "...", "method": "handle_error_action", "params": {"action_id": "open_privacy_settings"}}
// →
{"id": "...", "ok": true, "result": {"executed": true, "side_effect": "opened_settings"}}

// Active LLM HTTP probe — для HealthMonitor extension (B.1)
{"id": "...", "method": "probe_llm_http", "params": {}}
// →
{"id": "...", "ok": true, "result": {"reachable": true, "latency_ms": 11000, "model": "gemma-4-e4b-it-mlx"}}

// Manual clear errors (для Diagnostics tab "Clear all" button)
{"id": "...", "method": "clear_recent_errors", "params": {}}
```

**Backward compat:** все методы additive, существующие consumers не ломаются. Event consumers без подписки на `"krab_error"` его игнорируют. Phase A clients продолжают работать без изменений.

### Layer 3 — Active probe extension в HealthMonitor (Swift)

Phase A `HealthMonitor` сейчас пингает только `handle_ping` каждые 3 s. Phase B расширяет:

- **LLM HTTP probe** — каждые 30 s через IPC `probe_llm_http`. Если `reachable=false` → `error_bus.push(rewriter.unavailable, severity=warn, dedupe=300s)`. Если переходит false → true → emit `info` event "rewriter recovered" (severity=info, не показывает toast, только Diagnostics).
- **History write probe** — каждые 60 s. Backend пытается записать пустой sentinel в `history.lock` (touch + remove). Fail → `history.write_fail` critical.
- **Optional HF token probe** — каждые 5 минут когда diarization включён. Backend проверяет наличие token в env. Без token → `diarization.no_token` warn.

Эти три probe — **отдельный таск** в `HealthMonitor`, не блокируют main IPC ping. Реализовано через `Task.detached { while !cancelled { try await probe(); try await Task.sleep(...) } }`.

### Layer 4 — Error UI (Swift)

#### 4.1 ErrorToastView

Новый `native/KrabEarAgent/Sources/KrabEarAgent/ErrorToastView.swift`.

Severity-aware behaviour:
- `info` — light fade-in/out, 2 s, без звука
- `warn` — yellow accent, 5 s, без звука
- `error` — orange accent, 10 s, мягкий tone
- `critical` — red accent, manual dismiss only, system alert sound

Если `actionable=true`: добавляется button (компактный) с подписью из `action_label` registry. Нажатие → IPC `handle_error_action` → toast dismiss.

Queue: если в буфере >1 toast, показывается сверху последний; остальные накапливаются и видны в Diagnostics tab (не теряются). Maximum показ on-screen — один toast (плюс свой собственный StatusIndicator dot для long-lived states).

**Дизайн (visuals):** свой Gemini design pass в B.3 — backend готовит токены и поведение, frontend layout/colors определяет Gemini per `feedback_frontend_gemini.md`. Для B.1+B.2 используется минимальный Liquid Glass toast (re-use of `KrabEarTheme` ThemeCardView).

#### 4.2 StatusIndicator extension

Phase A StatusIndicator показывает только supervisor state (зелёный/жёлтый/красный по health). Phase B добавляет **layered colour**:

- Background = supervisor state (Phase A logic неизменна)
- Foreground badge = highest active KrabError severity:
  - `critical` → красный mini-dot поверх (blinking 0.5 Hz)
  - `error` → orange mini-dot (steady)
  - `warn` → yellow mini-dot (steady)
  - `info` → no badge (только в Diagnostics tab)

Tooltip показывает «N active issues — open Diagnostics».

#### 4.3 Diagnostics tab

Отдельная **вкладка** в главном окне (per parent spec line 159, не collapsible section). Layout:

- Header: severity filter chips (4 — all/info/warn/error/critical) + component filter dropdown
- List: last 200 errors, sortable by timestamp / severity / component
- Each row: timestamp + severity icon + component + message_user + (button «Fix» если actionable)
- Footer:
  - «Clear all» button (calls `clear_recent_errors`)
  - «Copy to clipboard» (selected rows or all)
  - «Open log file» (opens `~/Library/Application Support/KrabEar/backend.log` в Console.app)

**Persistance:** Diagnostics list полностью in-memory в backend (ring buffer 200). При рестарте backend — список сбрасывается, в `backend.log` всё остаётся (там полный history).

**B.3 design pass:** Layout/spacing/colors/iconography → Gemini design brief, потом Swift apply. Это отдельный mini-cycle: brief → JSON tokens → Swift.

### Layer 5 — Action handlers

Single source of truth — `KrabEar/backend/error_actions.py`:

```python
ACTION_HANDLERS = {
    "open_privacy_settings": _open_privacy_settings,
    "open_hf_token_setting": _open_hf_token_setting,
    "disable_rewriter": _disable_rewriter,
    "open_hotkey_settings": _open_hotkey_settings,
    "kill_lm_studio_via_telegram": _kill_lm_studio_via_telegram,  # feature-flagged
    "open_log_file": _open_log_file,
    "switch_to_balanced_profile": _switch_to_balanced_profile,
    "retry_history_save": _retry_history_save,
}

def handle_action(action_id: str, settings_service, ...) -> dict:
    handler = ACTION_HANDLERS.get(action_id)
    if not handler:
        raise ValueError(f"unknown action_id: {action_id}")
    return handler(...)
```

Большинство actions — **side-effect dispatchers**: `_open_privacy_settings` шлёт IPC event который Swift ловит и делает `NSWorkspace.open(...)`. `_disable_rewriter` пишет в settings `llm_rewrite_enabled=false`. `_kill_lm_studio_via_telegram` — feature-flagged, в B.1+B.2 возвращает `{"executed": false, "reason": "feature_disabled"}`, реальная Telegram bridge integration отложена.

---

## Error Registry (полный seed для всех 9 классов)

```python
# KrabEar/backend/error_codes.py

ERROR_REGISTRY: dict[str, dict] = {
    # ── Layer: paste ─────────────────────────────────────────────
    "paste.ax_denied": {
        "user_msg_ru": "Не смог вставить — текст в clipboard, нажми Cmd+V",
        "actionable": True,
        "action_id": "open_privacy_settings",
        "action_label": "Открыть Privacy Settings",
        "severity": "error",
        "dedupe_seconds": 60,
    },
    "paste.app_unsupported": {
        "user_msg_ru": "Эта программа не поддерживает paste — текст в clipboard",
        "actionable": False,
        "action_id": None,
        "severity": "info",
        "dedupe_seconds": 30,
    },

    # ── Layer: rewriter ──────────────────────────────────────────
    "rewriter.timeout": {
        "user_msg_ru": "Rewriter недоступен — raw text вставлен",
        "actionable": True,
        "action_id": "disable_rewriter",
        "action_label": "Выключить rewriter",
        "severity": "warn",
        "dedupe_seconds": 60,
    },
    "rewriter.connection_error": {
        "user_msg_ru": "LM Studio не отвечает — raw text вставлен",
        "actionable": True,
        "action_id": "disable_rewriter",
        "action_label": "Выключить rewriter",
        "severity": "warn",
        "dedupe_seconds": 60,
    },
    "rewriter.circuit_open": {
        "user_msg_ru": "Rewriter временно отключён после нескольких ошибок",
        "actionable": False,
        "action_id": None,
        "severity": "warn",
        "dedupe_seconds": 300,
    },
    "rewriter.unavailable": {  # active probe failure
        "user_msg_ru": "LM Studio недоступен (active probe)",
        "actionable": False,
        "action_id": None,
        "severity": "info",
        "dedupe_seconds": 300,
    },

    # ── Layer: stt ───────────────────────────────────────────────
    "stt.load_fail": {
        "user_msg_ru": "Не загрузилась STT модель — переключаюсь на balanced",
        "actionable": True,
        "action_id": "switch_to_balanced_profile",
        "action_label": "Переключить на balanced",
        "severity": "error",
        "dedupe_seconds": 30,
    },
    "stt.empty_text": {
        "user_msg_ru": "Тишина — ничего не распознано",
        "actionable": False,
        "action_id": None,
        "severity": "info",
        "dedupe_seconds": 5,
    },

    # ── Layer: diarization ───────────────────────────────────────
    "diarization.no_token": {
        "user_msg_ru": "Diarization недоступна — нужен HF token",
        "actionable": True,
        "action_id": "open_hf_token_setting",
        "action_label": "Указать токен",
        "severity": "warn",
        "dedupe_seconds": 600,  # 10 min — не спамим если token надолго removed
    },
    "diarization.pipeline_fail": {
        "user_msg_ru": "Diarization упала — записано как один спикер",
        "actionable": False,
        "action_id": None,
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # ── Layer: translation ───────────────────────────────────────
    "translation.timeout": {
        "user_msg_ru": "Перевод недоступен — оригинал сохранён",
        "actionable": False,
        "action_id": None,
        "severity": "warn",
        "dedupe_seconds": 60,
    },

    # ── Layer: mlx ───────────────────────────────────────────────
    "mlx.oom": {
        "user_msg_ru": "Не хватило памяти — выгрузи LM Studio или другие MLX-приложения",
        "actionable": True,
        "action_id": "kill_lm_studio_via_telegram",  # feature-flagged
        "action_label": "Выгрузить через Telegram",
        "severity": "critical",
        "dedupe_seconds": 5,
    },

    # ── Layer: history ───────────────────────────────────────────
    "history.write_fail": {
        "user_msg_ru": "Не удалось сохранить транскрипт — данные в tmpfile",
        "actionable": True,
        "action_id": "retry_history_save",
        "action_label": "Повторить сохранение",
        "severity": "critical",
        "dedupe_seconds": 10,
    },

    # ── Layer: vocabulary ────────────────────────────────────────
    "vocabulary.load_fail": {
        "user_msg_ru": "Не загрузился словарь — STT работает без bias",
        "actionable": False,
        "action_id": None,
        "severity": "warn",
        "dedupe_seconds": 600,
    },

    # ── Layer: hotkey ────────────────────────────────────────────
    "hotkey.conflict": {
        "user_msg_ru": "Right Option занят другим приложением",
        "actionable": True,
        "action_id": "open_hotkey_settings",
        "action_label": "Сменить hotkey",
        "severity": "warn",
        "dedupe_seconds": 300,
    },
}
```

**Total: 15 codes** (paste 2, rewriter 4, stt 2, diarization 2, translation 1, mlx 1, history 1, vocabulary 1, hotkey 1 = 9 user-facing классов из catalog'а + 6 sub-variants для finer-grained UX). Каждый дополнительный sub-variant требует отдельный регрессионный тест.

---

## Sentry / GlitchTip tier mapping

| Severity | Local log | Diagnostics tab | Sentry / GlitchTip |
|---|---|---|---|
| `info` | ✅ | ✅ (filter optional) | ❌ (quota — не шлём) |
| `warn` | ✅ | ✅ | ✅ batched: 10 events / 30 s window per code |
| `error` | ✅ | ✅ default view | ✅ immediate |
| `critical` | ✅ | ✅ + modal alert | ✅ immediate + breadcrumb context (last 20 events) |

**Privacy invariant** (продолжение существующего правила): never отправляем transcript text в Sentry. Только metadata: model name, latency_ms, exception class, error code. PII-safe.

**Release tagging:** уже работает per PR #241. Phase B tag добавляет `phase=b` + `code=<error_code>` для group'инга в dashboard.

---

## Active LLM HTTP probe (новый компонент)

### Backend side — `backend/llm_probe.py`

```python
class LLMHttpProbe:
    """Active health check for LM Studio. NOT the rewriter circuit.
    
    Lives independently in BackendService, runs in own thread, polls every 30s.
    Pushes error_bus events on state transitions, NOT on every poll.
    """
    
    def __init__(self, rewriter, error_bus, settings, interval_sec: float = 30.0):
        self._rewriter = rewriter
        self._error_bus = error_bus
        self._settings = settings
        self._interval = interval_sec
        self._last_state: Optional[bool] = None  # None / True / False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="LLMHttpProbe")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.wait(self._interval):
            if not self._settings.get("llm_rewrite_enabled", False):
                continue
            current = self._rewriter.warmup()  # already-existing PR #362 method, no circuit impact
            if current != self._last_state:
                self._on_state_change(self._last_state, current)
            self._last_state = current

    def _on_state_change(self, old: Optional[bool], new: bool):
        if new is False:
            self._error_bus.push(KrabError(
                severity="info",
                component="rewriter",
                code="rewriter.unavailable",
                message_user="LM Studio недоступен (active probe)",
                message_debug=f"warmup() returned False; transitioning {old}→{new}",
                timestamp=datetime.now(),
                context={"model": self._rewriter._model},
                actionable=False,
                action_id=None,
            ))
        # На recovery (False→True) — info event не нужен в toast,
        # но event_bus получит для Diagnostics tab «recovered» row.
        elif old is False and new is True:
            self._event_bus.emit("rewriter_recovered", {
                "ts": datetime.now().isoformat(),
                "latency_ms": self._rewriter._last_latency_ms,
            })
```

### Why separate from circuit breaker

CircuitBreaker управляет user-facing call paths — открыт ⇒ отдаёт raw fallback. Probe — independent observability канал, не влияет на circuit state. Это важно: если probe сам начнёт резетить circuit, мы получим race на каждом cold-load (probe success → circuit close → real call → timeout → circuit open → loop).

### Hooks into HealthMonitor (Swift side)

`HealthMonitor.swift` (Phase A) — это actor который пингает IPC. Phase B расширяет:

```swift
extension HealthMonitor {
    func subscribeToProbeEvents(eventBus: EventBusClient) {
        eventBus.subscribe(eventType: "rewriter_recovered") { [weak self] data in
            await self?.statusIndicator.flashGreen(reason: "rewriter recovered")
        }
        // Subscribe to other downstream probes as they're added
    }
}
```

---

## Staged delivery

### B.1 — Core ErrorBus + 3 errors + active LLM probe (3-5 дней)

**Scope:**
- `backend/error_bus.py` (ErrorBus + KrabError) — новый
- `backend/error_codes.py` (registry seed для 16 codes выше) — новый
- `backend/error_actions.py` (handler stubs для всех action_id, real impl только для `open_privacy_settings`, `open_hf_token_setting`, `disable_rewriter`) — новый
- `backend/llm_probe.py` (LLMHttpProbe) — новый
- `backend/service.py` — wire ErrorBus + probe в `BackendService.__init__`, expose 4 IPC методов (`list_recent_errors`, `handle_error_action`, `probe_llm_http`, `clear_recent_errors`)
- `backend/llm_rewriter.py` — заменить три `logger.warning(...)` ветки (которые добавил PR #362) на `error_bus.push(...)` рядом (logger остаётся)
- `backend/transcriber.py` — wire в `paste.ax_denied`, `stt.empty_text`
- `backend/recorder.py` — wire `paste.app_unsupported` (если есть placeholder)
- `native/KrabEarAgent/Sources/KrabEarAgent/ErrorToastView.swift` — новый, минимальный Liquid Glass toast
- `native/KrabEarAgent/Sources/KrabEarAgent/ErrorActionHandler.swift` — новый, Swift side dispatcher
- `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift` — extend (subscribeToProbeEvents)
- `native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicatorView.swift` — extend (foreground severity badge)
- `native/KrabEarAgent/Sources/KrabEarAgent/main+Errors.swift` — новый, wire ErrorBus subscription в startup
- `KrabEar/tests/test_error_bus.py` — новый
- `KrabEar/tests/test_error_codes.py` — новый
- `KrabEar/tests/test_llm_probe.py` — новый
- `KrabEar/tests/test_error_actions.py` — новый

**Acceptance criteria B.1:**
1. Revoke Accessibility permission → dictate → toast «Не смог вставить — Cmd+V» появляется в течение 1 s после paste attempt + clipboard содержит текст + button «Открыть Privacy Settings» работает.
2. Stop LM Studio → wait 60 s → active probe детектит → `rewriter.unavailable` event в Diagnostics list (info severity, без toast — мы не хотим спама когда rewriter ещё не нужен) → restart LM Studio → `rewriter_recovered` event + green flash StatusIndicator.
3. Dictate → rewriter timeout (симулируется через **новую testing-only env var** `KRAB_EAR_LLM_FORCE_TIMEOUT=1` — добавляется в B.1 scope, читается в `LLMRewriter._rewrite_impl` для inject'а `requests.Timeout`) → toast «Rewriter недоступен — raw text вставлен» + raw text реально в paste destination + button «Выключить rewriter» меняет setting и dismiss'ит toast.
4. Remove HF token → enable diarization → dictate → toast «Diarization недоступна — нужен HF token» + button «Указать токен» открывает Settings → HF Token field focused.
5. Все три error events видны в Diagnostics tab list (даже если Diagnostics tab UI ещё минимальная — этот пункт можно проверить через IPC `list_recent_errors`).

**Validation checkpoint:** после B.1 merge — 1-2 дня observation. Проверяем:
- KrabError shape удобный? (нет ли пропущенных полей вроде `recurrence_count`)
- Dedupe windows подходят? (paste 60 s слишком long? слишком short?)
- Active probe interval 30 s OK? (батарея, throttle?)
- StatusIndicator badge не отвлекает?

Если что-то требует структурного изменения — корректируем spec, **не B.2**.

### B.2 — Inline остальные 6 классов (1-2 дня)

После validation checkpoint без structural изменений добавляем `error_bus.push(...)` в:
- `backend/translator.py` — `translation.timeout`
- `backend/state_store.py` — `history.write_fail` (+ tmpfile retry в `error_actions.py`)
- `backend/transcriber.py` — `stt.load_fail` (с auto-fallback на balanced — already partially implemented в существующем fallback chain, добавляем error event)
- `backend/vocabulary_store.py` — `vocabulary.load_fail`
- `core/engine.py` — `mlx.oom` (через signal handler / process exit code)
- `native/KrabEarAgent/Sources/KrabEarAgent/HotkeyManager.swift` — `hotkey.conflict` (когда `RegisterEventHotKey` возвращает eventHotKeyExistsErr)

**Acceptance criteria B.2:**
- Все 16 codes из registry имеют either real call site или regression test stub.
- `mlx.oom` ловится через `os.WTERMSIG(status)==SIGABRT` или OOM signature в stderr — детектор работает в integration test.
- `history.write_fail` → tmpfile fallback восстанавливается через action button.

### B.3 — Diagnostics tab (отдельный design pass)

Реализация UI после Gemini design brief.

**Brief content (для Gemini):**
- Цель: показать last 200 KrabError с фильтрами severity/component/component
- Constraints: macOS Sequoia liquid glass, dark mode, 800×600 минимальный размер
- Tokens: уже существующий `KrabEarTheme` palette
- Interactions: row hover, severity chip click toggle, component dropdown, copy-to-clipboard, open-log-file
- Empty state: «Нет ошибок — всё работает» (текст без emoji per CLAUDE.md правило)
- Loading state: skeleton rows

**Output:** `tokens.json` + Swift apply patch.

**Files:**
- `native/KrabEarAgent/Sources/KrabEarAgent/DiagnosticsTabView.swift` — новый
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Diagnostics.swift` — новый extension
- `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` — wire tab в main window

**Acceptance criteria B.3:**
- Tab появляется в главном окне.
- Filter chips работают (toggle severity).
- Component dropdown filter работает.
- Copy-to-clipboard копирует selected rows JSON.
- Open log file открывает `~/Library/Application Support/KrabEar/backend.log` в Console.app.
- Empty state корректно отображается при clear.

---

## Testing strategy

| Level | Coverage | Tooling |
|---|---|---|
| Unit | ErrorBus dedupe / push / sentry routing; KrabError validation; ACTION_HANDLERS dispatch | pytest |
| Unit (Swift) | ErrorToastView severity dispatch / queue; StatusIndicator badge layering | XCTest |
| Integration | error_bus.push → event_bus.emit → IPC SSE event → Swift subscribe | pytest + UnixSocketTestHarness |
| Integration | LLMHttpProbe state transitions (alive→dead→alive) → events emitted | pytest with mocked HTTP server |
| End-to-end | Revoke AX → dictate → toast appears → click action → settings open | manual + AppleScript via XCUITest |
| Regression | Each registry code has at least one push() call site test | pytest collected list |

**CI extension:** добавляем `test_error_bus.py`, `test_error_codes.py`, `test_llm_probe.py`, `test_error_actions.py` в существующий backend-tests workflow. Swift tests добавляем в KrabEarAgentTests target.

**Performance budget:** ErrorBus.push() must be <0.5 ms p99 (just dedupe lookup + dict put + event_bus.emit). Active LLM probe — 30 s interval × ~50 ms warmup → 0.16 % CPU. Negligible.

---

## Backward compatibility

- All new IPC methods additive — old clients игнорируют unknown event types (`"krab_error"`, `"rewriter_recovered"`).
- Existing `event_bus.emit` API unchanged.
- Settings additions:
  - `error_bus_enabled: bool = True` (kill switch)
  - `error_dedupe_window_sec: float = 30.0` (override default)
  - `llm_probe_enabled: bool = True`
  - `llm_probe_interval_sec: float = 30.0`
  - `diagnostics_tab_enabled: bool = True`
  - `error_severity_threshold: Severity = "info"` — minimum severity для UI display
- Defaults preserve current behaviour — если user отключает все четыре settings, поведение === current (Phase A only).
- `logger.warning(...)` от PR #362 остаётся параллельно с `error_bus.push(...)`. Не дублируем сообщение в Sentry — ErrorBus сам решает Sentry routing, а logger.warning только в local log.

---

## Risks

| Risk | Mitigation |
|---|---|
| Toast fatigue (слишком много toast'ов) | Severity-aware UI + per-code dedupe + Diagnostics tab consolidation. Feature flag `error_bus_enabled=false` как kill-switch. |
| Active probe держит LM Studio загруженной 24/7 (повышение idle RAM) | `llm_probe_enabled` user-controlled, default ON. Probe = `warmup()` с `max_tokens=1`. **Поведение зависит от LM Studio JIT settings:** если auto-eviction off — probe просто keep-alive, no reload. Если auto-eviction on — probe **может** триггернуть reload каждые 30 s (это плохо: лишние cold-load cycles). **Mitigation:** detect cold-load via `latency_ms > 3000` → automatically increase interval до 5 min для следующих poll'ов; recover к 30 s после трёх consecutive fast responses. Если user всё ещё не хочет хранить gemma в RAM — выключает probe или sets longer base interval вручную. |
| Sentry quota exhaustion (warn batching не справится) | Tier mapping: info=local-only, warn batched 10/30s, error/critical immediate. Plus existing PR #241 release tagging для group'инга. Если quota всё равно горит — increase batch window до 60 s. |
| ErrorBus.push() в hot path (transcription pipeline) | Push'ы — рядкие events, не на каждый sample. Profiler smoke test перед merge. |
| Action handler open_privacy_settings не работает на macOS 26+ (deep link API mutation) | Каждый `open_*` action имеет fallback URL plus integration test on CI macos-latest. |
| KrabError shape оказывается недостаточным (нужен `recurrence_count` или `affected_user_action`) | B.1 validation checkpoint — корректируем перед B.2. Pydantic model migration backward-compatible через `Optional[int] = None`. |

---

## Open questions resolved

1. **Sentry quota policy** → resolved per table выше: info local, warn batched, error/critical immediate.
2. **Dedupe window** → resolved 30 s default, per-code override в registry. Конкретные значения для всех 16 codes выше.
3. **Diagnostics tab — отдельная вкладка или collapsible?** → отдельная вкладка (per parent spec). Реализация в B.3 после Gemini design pass.
4. **Telegram bridge для kill_lm_studio** → отложено за feature flag. В B.1+B.2 action возвращает `feature_disabled`. Реальная Telegram bridge реализация — separate spec, depends on main Krab bot API.

## Open questions remaining

1. **launchd plist auto-install on first run** — out of scope for B, parent spec mentions это как user pref question. Решается отдельно.
2. **Error event persistence** — должен ли ring buffer 200 переживать backend restart? Текущее предложение — нет (in-memory only). Если user хочет full forensics — backend.log уже всё пишет. Решение: оставить in-memory, можно вернуться при появлении use case.

---

## Files (consolidated)

```
KrabEar/backend/
  error_bus.py                     [B.1 new]
  error_codes.py                   [B.1 new — registry seed]
  error_actions.py                 [B.1 new — dispatcher]
  llm_probe.py                     [B.1 new — active LLM HTTP probe]
  llm_rewriter.py                  [B.1 modified — replace logger.warning with parallel error_bus.push]
  transcriber.py                   [B.1 modified, B.2 extended]
  service.py                       [B.1 modified — wire ErrorBus + 4 new IPC methods]
  recorder.py                      [B.1 modified — paste codes]
  translator.py                    [B.2 modified]
  state_store.py                   [B.2 modified]
  vocabulary_store.py              [B.2 modified]

KrabEar/core/
  engine.py                        [B.2 modified — mlx.oom signal handler]

native/KrabEarAgent/Sources/KrabEarAgent/
  ErrorToastView.swift             [B.1 new]
  ErrorActionHandler.swift         [B.1 new]
  StatusIndicatorView.swift        [B.1 modified — severity badge]
  HealthMonitor.swift              [B.1 modified — subscribeToProbeEvents]
  HotkeyManager.swift              [B.2 modified — hotkey.conflict]
  main+Errors.swift                [B.1 new — startup wiring]
  DiagnosticsTabView.swift         [B.3 new — after Gemini design pass]
  HistoryPanelController+Diagnostics.swift  [B.3 new extension]
  main.swift                       [B.3 modified — wire diagnostics tab]

KrabEar/tests/
  test_error_bus.py                [B.1 new]
  test_error_codes.py              [B.1 new]
  test_error_actions.py            [B.1 new]
  test_llm_probe.py                [B.1 new]
  test_error_bus_integration.py    [B.1 new — full event flow]
  (existing test files extended in B.2 for new push call sites)
```

---

## Next steps

1. **User reviews this spec** — please read top-to-bottom, flag anything unclear or wrong.
2. After approval → invoke `superpowers:writing-plans` skill → создать **detailed implementation plan для B.1 specifically** (с pre-merge checklist, test commands, rollback plan).
3. Plan B.1 → executing-plans → ship → 1-2 day observation checkpoint.
4. If checkpoint OK → invoke writing-plans для B.2.
5. After B.2 ship → отдельный Gemini design brief → invoke writing-plans для B.3.

---

*End of design.*
