# Phase B.1 — Loud Errors Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the core Error Bus architecture (Python + Swift), wire 3 user-facing error classes (paste.ax_denied, rewriter.timeout, diarization.no_token), and add active LLM HTTP probe to HealthMonitor — closing today's silent-failure gap.

**Architecture:** Python `error_bus.py` (Pydantic `KrabError` + dedupe ring buffer + Sentry tier router) feeds into existing `event_bus` SSE stream consumed by Swift `ErrorActionHandler` + `ErrorToastView`. Active `LLMHttpProbe` thread polls LM Studio `/v1/chat/completions` warmup independently of the user-facing rewrite path. Status indicator dot gains a layered severity badge.

**Tech Stack:** Python 3.14, Pydantic v2, `requests`, threading, pytest, Swift 6 (macOS 13+), XCTest, NSVisualEffectView.

**Spec:** `docs/superpowers/specs/2026-05-04-phase-b-loud-errors-design.md`

**Branch base:** `codex/krab-ear-v2` (rebase after PR #362 + 503 tests merge land — both touch `llm_rewriter.py` and `test_llm_rewriter.py`)

**New branch:** `feat/phase-b1-loud-errors-core-2026-05-04`

---

## Pre-flight

- [ ] **Check PR #362 + 503 tests merged into `codex/krab-ear-v2`**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git fetch origin
git log origin/codex/krab-ear-v2 --oneline -10 | grep -E "rewriter resilience|503 JIT retry"
```

Expected: both commits present. If not, ask user to merge them first or set this plan to rebase later.

- [ ] **Verify Phase A is in place**

```bash
git ls-files native/KrabEarAgent/Sources/KrabEarAgent | grep -E "HealthMonitor|BackendToast|StatusIndicator|BackendSupervisor"
```

Expected: 4 files listed.

- [ ] **Sanity build**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_llm_rewriter.py -q 2>&1 | tail -5
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3
```

Expected: 79 pytest pass, swift build succeeds.

- [ ] **Create branch**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git checkout -b feat/phase-b1-loud-errors-core-2026-05-04 origin/codex/krab-ear-v2
```

---

## File Structure

| File | Status | Purpose | Lines (approx) |
|---|---|---|---|
| `KrabEar/backend/error_bus.py` | new | `KrabError` Pydantic + `ErrorBus` (dedupe, ring buffer, Sentry routing) + `WarnBatcher` | 220 |
| `KrabEar/backend/error_codes.py` | new | `ERROR_REGISTRY` dict (15 codes) + `Severity` re-export | 130 |
| `KrabEar/backend/error_actions.py` | new | `ACTION_HANDLERS` dispatch table + 3 real handlers + stubs | 110 |
| `KrabEar/backend/llm_probe.py` | new | `LLMHttpProbe` thread with state-transition events + adaptive interval | 130 |
| `KrabEar/backend/service.py` | modified | wire `ErrorBus` + `LLMHttpProbe` in `__init__`; add 4 IPC methods | +90 |
| `KrabEar/backend/llm_rewriter.py` | modified | parallel `error_bus.push` next to existing `logger.warning` (3 paths); env var `KRAB_EAR_LLM_FORCE_TIMEOUT` | +35 |
| `KrabEar/backend/transcriber.py` | modified | push `paste.ax_denied`, `diarization.no_token` | +20 |
| `KrabEar/tests/test_error_bus.py` | new | unit tests for `KrabError`, `ErrorBus.push`, dedupe, ring, Sentry routing | 280 |
| `KrabEar/tests/test_error_codes.py` | new | registry shape + dedupe_seconds present | 60 |
| `KrabEar/tests/test_error_actions.py` | new | dispatcher + 3 real handlers, stubs return `feature_disabled` | 120 |
| `KrabEar/tests/test_llm_probe.py` | new | state machine, adaptive interval, integration with mock rewriter | 200 |
| `KrabEar/tests/test_error_bus_integration.py` | new | full flow: push → event_bus → IPC list_recent_errors | 120 |
| `KrabEar/tests/test_llm_rewriter.py` | modified | assert `error_bus.push` called in 3 failure branches | +60 |
| `native/KrabEarAgent/Sources/KrabEarAgent/ErrorToastView.swift` | new | severity-aware toast UI (Liquid Glass) | 180 |
| `native/KrabEarAgent/Sources/KrabEarAgent/ErrorActionHandler.swift` | new | parses `krab_error` SSE events, dispatches actions to backend, opens settings | 140 |
| `native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicatorView.swift` | modified | layered severity badge over Phase A health dot | +50 |
| `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift` | modified | `subscribeToProbeEvents(eventBus:)` reacts to `rewriter_recovered` | +30 |
| `native/KrabEarAgent/Sources/KrabEarAgent/main+Errors.swift` | new | wire `ErrorActionHandler` into startup after IPC ready | 80 |
| `native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorToastViewTests.swift` | new | severity dispatch, dedupe, queue depth | 100 |
| `native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorActionHandlerTests.swift` | new | event parsing, action dispatch | 110 |

Test infra additions: env var `KRAB_EAR_LLM_FORCE_TIMEOUT=1` understood by `LLMRewriter._rewrite_impl` for E2E rewriter timeout simulation.

---

## Tasks

### Task 1: KrabError Pydantic model + Severity literal

**Files:**
- Create: `KrabEar/backend/error_bus.py`
- Test: `KrabEar/tests/test_error_bus.py`

- [ ] **Step 1.1: Write failing test for `KrabError` validation**

Create `KrabEar/tests/test_error_bus.py`:

```python
import unittest
from datetime import datetime, timezone

import pytest

from backend.error_bus import KrabError


class KrabErrorModelTests(unittest.TestCase):
    def test_minimal_valid_construction(self):
        err = KrabError(
            severity="warn",
            component="rewriter",
            code="rewriter.timeout",
            message_user="Rewriter недоступен",
            message_debug="HTTP timeout after 45s",
            timestamp=datetime.now(timezone.utc),
            context={"model": "gemma"},
            actionable=False,
            action_id=None,
        )
        self.assertEqual(err.severity, "warn")
        self.assertIsNone(err.action_id)

    def test_invalid_severity_rejected(self):
        with pytest.raises(Exception):
            KrabError(
                severity="catastrophic",  # not in Literal
                component="rewriter",
                code="rewriter.timeout",
                message_user="x",
                message_debug="x",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

    def test_invalid_component_rejected(self):
        with pytest.raises(Exception):
            KrabError(
                severity="warn",
                component="nonexistent",  # not in Literal
                code="x.y",
                message_user="x",
                message_debug="x",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

    def test_model_dump_json_mode_serialises_datetime(self):
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        err = KrabError(
            severity="info",
            component="stt",
            code="stt.empty_text",
            message_user="x",
            message_debug="x",
            timestamp=ts,
            context={"k": "v"},
            actionable=False,
            action_id=None,
        )
        dumped = err.model_dump(mode="json")
        self.assertEqual(dumped["timestamp"], "2026-05-04T12:00:00+00:00")
```

- [ ] **Step 1.2: Run test and verify it fails**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_bus.py::KrabErrorModelTests -v 2>&1 | tail -10
```

Expected: ImportError or "module backend.error_bus not found".

- [ ] **Step 1.3: Implement `KrabError` and `Severity`**

Create `KrabEar/backend/error_bus.py`:

```python
"""Structured error bus for surfacing silent failures to the UI.

KrabError is a Pydantic model. ErrorBus is a thread-safe pusher that
dedupes per-code, keeps a ring buffer for the Diagnostics tab, and routes
to Sentry by severity tier (info=skip, warn=batch, error/critical=immediate).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

logger = logging.getLogger("KrabEar.Backend.ErrorBus")

Severity = Literal["info", "warn", "error", "critical"]
Component = Literal[
    "stt", "rewriter", "paste", "diarization",
    "translation", "mlx", "history", "vocabulary", "hotkey",
]


class KrabError(BaseModel):
    severity: Severity
    component: Component
    code: str
    message_user: str
    message_debug: str
    timestamp: datetime
    context: dict
    actionable: bool
    action_id: Optional[str]
```

- [ ] **Step 1.4: Run test and verify it passes**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_bus.py::KrabErrorModelTests -v 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 1.5: Commit**

```bash
git add KrabEar/backend/error_bus.py KrabEar/tests/test_error_bus.py
git commit -m "feat(error_bus): KrabError Pydantic model with Severity/Component literals"
```

---

### Task 2: ERROR_REGISTRY (15 codes) + sanity tests

**Files:**
- Create: `KrabEar/backend/error_codes.py`
- Test: `KrabEar/tests/test_error_codes.py`

- [ ] **Step 2.1: Write failing test for registry shape**

Create `KrabEar/tests/test_error_codes.py`:

```python
import unittest

from backend.error_codes import ERROR_REGISTRY


class ErrorRegistryShapeTests(unittest.TestCase):
    REQUIRED_KEYS = {
        "user_msg_ru", "actionable", "action_id",
        "action_label", "severity", "dedupe_seconds",
    }
    VALID_SEVERITIES = {"info", "warn", "error", "critical"}

    def test_all_entries_have_required_keys(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                missing = self.REQUIRED_KEYS - set(entry.keys())
                self.assertFalse(missing, f"{code} missing keys: {missing}")

    def test_severities_valid(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                self.assertIn(entry["severity"], self.VALID_SEVERITIES)

    def test_actionable_implies_action_id(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                if entry["actionable"]:
                    self.assertIsNotNone(entry["action_id"], f"{code} actionable but no action_id")
                    self.assertTrue(entry["action_label"], f"{code} actionable but empty action_label")

    def test_dedupe_seconds_positive(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                self.assertGreater(entry["dedupe_seconds"], 0)

    def test_expected_codes_present(self):
        expected = {
            "paste.ax_denied", "paste.app_unsupported",
            "rewriter.timeout", "rewriter.connection_error",
            "rewriter.circuit_open", "rewriter.unavailable",
            "stt.load_fail", "stt.empty_text",
            "diarization.no_token", "diarization.pipeline_fail",
            "translation.timeout",
            "mlx.oom",
            "history.write_fail",
            "vocabulary.load_fail",
            "hotkey.conflict",
        }
        self.assertEqual(set(ERROR_REGISTRY.keys()), expected)
```

- [ ] **Step 2.2: Run and verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_codes.py -v 2>&1 | tail -10
```

Expected: ImportError.

- [ ] **Step 2.3: Implement registry**

Create `KrabEar/backend/error_codes.py` with the full 15-code registry from the spec (`docs/superpowers/specs/2026-05-04-phase-b-loud-errors-design.md`, "Error Registry" section). Copy verbatim — do not paraphrase user-facing strings.

```python
"""Single source of truth for error code definitions.

Adding a new code:
1. Add entry here with required keys.
2. Add a regression test in test_error_codes.py.
3. Wire `error_bus.push(KrabError(code="...", ...))` at the call site.
4. If actionable, add a real handler in error_actions.py.
"""
from typing import TypedDict, Optional


class _Entry(TypedDict):
    user_msg_ru: str
    actionable: bool
    action_id: Optional[str]
    action_label: str
    severity: str
    dedupe_seconds: int


ERROR_REGISTRY: dict[str, _Entry] = {
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
        "action_label": "",
        "severity": "info",
        "dedupe_seconds": 30,
    },
    # ... rewriter (4), stt (2), diarization (2), translation (1), mlx (1),
    # history (1), vocabulary (1), hotkey (1) — total 15.
    # Copy verbatim from spec to keep user_msg_ru wording stable.
}
```

Engineer note: keep entries sorted by component then code. Each entry has all 6 keys, even when `action_id` is `None` and `action_label` is `""`.

- [ ] **Step 2.4: Run tests and verify pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_codes.py -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 2.5: Commit**

```bash
git add KrabEar/backend/error_codes.py KrabEar/tests/test_error_codes.py
git commit -m "feat(error_codes): seed ERROR_REGISTRY with 15 codes (9 classes + 6 sub-variants)"
```

---

### Task 3: ErrorBus core (push, dedupe, ring buffer)

**Files:**
- Modify: `KrabEar/backend/error_bus.py`
- Modify: `KrabEar/tests/test_error_bus.py`

- [ ] **Step 3.1: Write failing tests for ErrorBus.push**

Append to `KrabEar/tests/test_error_bus.py`:

```python
import threading
import time
from unittest.mock import MagicMock

from backend.error_bus import ErrorBus
from backend.error_codes import ERROR_REGISTRY


class ErrorBusPushTests(unittest.TestCase):
    def setUp(self):
        self.event_bus = MagicMock()
        self.bus = ErrorBus(
            event_bus=self.event_bus,
            registry=ERROR_REGISTRY,
            sentry_client=None,
            default_dedupe_window_sec=30.0,
            ring_buffer_size=200,
        )

    def _make_err(self, code="rewriter.timeout", severity="warn"):
        return KrabError(
            severity=severity, component="rewriter", code=code,
            message_user="x", message_debug="y",
            timestamp=datetime.now(timezone.utc),
            context={}, actionable=False, action_id=None,
        )

    def test_push_emits_event(self):
        ok = self.bus.push(self._make_err())
        self.assertTrue(ok)
        self.event_bus.emit.assert_called_once()
        args = self.event_bus.emit.call_args
        self.assertEqual(args[0][0], "krab_error")
        payload = args[0][1]
        self.assertEqual(payload["code"], "rewriter.timeout")

    def test_push_dedupe_within_window(self):
        e1 = self._make_err()
        e2 = self._make_err()
        self.assertTrue(self.bus.push(e1))
        self.assertFalse(self.bus.push(e2))
        self.assertEqual(self.event_bus.emit.call_count, 1)

    def test_push_dedupe_per_code_window_from_registry(self):
        # paste.ax_denied has dedupe_seconds=60
        # paste.app_unsupported has dedupe_seconds=30
        # both pushed sequentially — different codes, both should pass
        e_ax = self._make_err(code="paste.ax_denied")
        e_ax.component = "paste"  # set via construction below for type match
        e_ax2 = KrabError(
            severity="error", component="paste", code="paste.ax_denied",
            message_user="x", message_debug="y",
            timestamp=datetime.now(timezone.utc),
            context={}, actionable=True, action_id="open_privacy_settings",
        )
        e_unsup = KrabError(
            severity="info", component="paste", code="paste.app_unsupported",
            message_user="x", message_debug="y",
            timestamp=datetime.now(timezone.utc),
            context={}, actionable=False, action_id=None,
        )
        self.assertTrue(self.bus.push(e_ax2))
        self.assertTrue(self.bus.push(e_unsup))
        self.assertFalse(self.bus.push(e_ax2))  # within 60s window
        self.assertEqual(self.event_bus.emit.call_count, 2)

    def test_ring_buffer_caps_at_max(self):
        bus = ErrorBus(
            event_bus=MagicMock(), registry=ERROR_REGISTRY,
            ring_buffer_size=3,
        )
        for i in range(5):
            err = KrabError(
                severity="info", component="stt", code=f"stt.empty_text",
                message_user=str(i), message_debug=str(i),
                timestamp=datetime.now(timezone.utc),
                context={"i": i}, actionable=False, action_id=None,
            )
            # bypass dedupe by using monotonic time travel: just call list_recent
            # and check ring is bounded
            bus.push(err)
            time.sleep(0.001)
        recent = bus.list_recent()
        self.assertLessEqual(len(recent), 3)

    def test_clear_returns_count(self):
        self.bus.push(self._make_err())
        n = self.bus.clear()
        self.assertGreaterEqual(n, 0)
        self.assertEqual(len(self.bus.list_recent()), 0)

    def test_thread_safety_smoke(self):
        # 10 threads × 100 pushes each — no exceptions, no crash
        def worker():
            for i in range(100):
                err = KrabError(
                    severity="info", component="stt",
                    code=f"stt.empty_text",
                    message_user="x", message_debug="x",
                    timestamp=datetime.now(timezone.utc),
                    context={"i": i}, actionable=False, action_id=None,
                )
                self.bus.push(err)
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        # Nothing to assert about counts (dedupe + ring), just no exception.
```

- [ ] **Step 3.2: Run, verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_bus.py::ErrorBusPushTests -v 2>&1 | tail -10
```

Expected: ImportError on `ErrorBus`.

- [ ] **Step 3.3: Implement `ErrorBus.push`, `list_recent`, `clear`, `_dedupe_window_for`**

Append to `KrabEar/backend/error_bus.py`:

```python
import threading
import time
from collections import deque

_SEVERITY_TO_LOG_LEVEL = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class ErrorBus:
    def __init__(
        self,
        event_bus,
        registry: dict,
        sentry_client=None,
        default_dedupe_window_sec: float = 30.0,
        ring_buffer_size: int = 200,
    ):
        self._event_bus = event_bus
        self._registry = registry
        self._sentry = sentry_client
        self._default_dedupe = default_dedupe_window_sec
        self._dedupe: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ring: deque = deque(maxlen=ring_buffer_size)

    def push(self, err: KrabError) -> bool:
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
        try:
            self._event_bus.emit("krab_error", err.model_dump(mode="json"))
        except Exception:
            logger.exception("event_bus.emit failed for code=%s", err.code)
        # Sentry routing added in Task 4
        return True

    def _dedupe_window_for(self, code: str) -> float:
        entry = self._registry.get(code, {})
        return float(entry.get("dedupe_seconds", self._default_dedupe))

    def list_recent(self, limit: int = 200) -> list[KrabError]:
        with self._lock:
            items = list(self._ring)
        return items[-limit:]

    def clear(self) -> int:
        with self._lock:
            n = len(self._ring)
            self._ring.clear()
            return n
```

- [ ] **Step 3.4: Run, verify pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_bus.py -v 2>&1 | tail -15
```

Expected: 10 passed.

- [ ] **Step 3.5: Commit**

```bash
git add KrabEar/backend/error_bus.py KrabEar/tests/test_error_bus.py
git commit -m "feat(error_bus): ErrorBus.push with dedupe, ring buffer, list_recent, clear"
```

---

### Task 4: Sentry routing + WarnBatcher

**Files:**
- Modify: `KrabEar/backend/error_bus.py`
- Modify: `KrabEar/tests/test_error_bus.py`

- [ ] **Step 4.1: Write failing tests for Sentry tier routing**

Append to `KrabEar/tests/test_error_bus.py`:

```python
class ErrorBusSentryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.sentry = MagicMock()
        self.bus = ErrorBus(
            event_bus=MagicMock(),
            registry=ERROR_REGISTRY,
            sentry_client=self.sentry,
        )

    def _err(self, code, severity, component="rewriter"):
        return KrabError(
            severity=severity, component=component, code=code,
            message_user="x", message_debug="dbg",
            timestamp=datetime.now(timezone.utc),
            context={"k": "v"}, actionable=False, action_id=None,
        )

    def test_info_skipped(self):
        self.bus.push(self._err("stt.empty_text", "info", "stt"))
        self.sentry.capture_message.assert_not_called()

    def test_error_immediate(self):
        self.bus.push(self._err("paste.ax_denied", "error", "paste"))
        self.sentry.capture_message.assert_called_once()
        kwargs = self.sentry.capture_message.call_args.kwargs
        self.assertEqual(kwargs["level"], "error")
        self.assertEqual(kwargs["tags"]["phase"], "b")
        self.assertEqual(kwargs["tags"]["code"], "paste.ax_denied")

    def test_critical_immediate(self):
        self.bus.push(self._err("mlx.oom", "critical", "mlx"))
        self.sentry.capture_message.assert_called_once()
        kwargs = self.sentry.capture_message.call_args.kwargs
        self.assertEqual(kwargs["level"], "critical")

    def test_warn_batched(self):
        # 9 warns in same code → no immediate send
        for i in range(9):
            err = self._err("rewriter.timeout", "warn")
            err.context = {"i": i}
            self.bus.push(err)
            # bypass dedupe by manually clearing the per-code timestamp
            self.bus._dedupe.pop("rewriter.timeout", None)
        self.sentry.capture_message.assert_not_called()

    def test_warn_batch_flush_at_10(self):
        for i in range(10):
            err = self._err("rewriter.timeout", "warn")
            err.context = {"i": i}
            self.bus.push(err)
            self.bus._dedupe.pop("rewriter.timeout", None)
        # 10th push triggers flush
        self.sentry.capture_message.assert_called()
```

- [ ] **Step 4.2: Run, verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_bus.py::ErrorBusSentryRoutingTests -v 2>&1 | tail -10
```

Expected: AssertionErrors (sentry not called even for error/critical).

- [ ] **Step 4.3: Add `WarnBatcher` and `_route_to_sentry`**

Append to `KrabEar/backend/error_bus.py`:

```python
class WarnBatcher:
    """Accumulate warn-level errors, flush every N events or every T seconds per code."""

    def __init__(self, sentry_client, batch_size: int = 10, window_sec: float = 30.0):
        self._sentry = sentry_client
        self._batch_size = batch_size
        self._window = window_sec
        self._buffer: dict[str, list[KrabError]] = {}
        self._first_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, err: KrabError) -> None:
        with self._lock:
            buf = self._buffer.setdefault(err.code, [])
            buf.append(err)
            if err.code not in self._first_seen:
                self._first_seen[err.code] = time.monotonic()
            should_flush = len(buf) >= self._batch_size or \
                (time.monotonic() - self._first_seen[err.code]) >= self._window
            if should_flush:
                self._flush_locked(err.code)

    def _flush_locked(self, code: str) -> None:
        buf = self._buffer.pop(code, [])
        self._first_seen.pop(code, None)
        if not buf or self._sentry is None:
            return
        latest = buf[-1]
        self._sentry.capture_message(
            f"{code} batched x{len(buf)}: {latest.message_debug}",
            level="warning",
            tags={"phase": "b", "code": code, "component": latest.component, "batch_size": len(buf)},
            extras={"latest_context": latest.context, "batch_count": len(buf)},
        )

    def flush_all(self) -> None:
        with self._lock:
            for code in list(self._buffer.keys()):
                self._flush_locked(code)
```

Modify `ErrorBus.__init__` to instantiate `self._warn_batcher` and `ErrorBus.push` to call `self._route_to_sentry(err)`:

```python
# In __init__:
self._warn_batcher = WarnBatcher(sentry_client) if sentry_client else None

# Add method:
def _route_to_sentry(self, err: KrabError) -> None:
    if self._sentry is None or err.severity == "info":
        return
    if err.severity == "warn":
        if self._warn_batcher:
            self._warn_batcher.add(err)
        return
    # error / critical — immediate
    self._sentry.capture_message(
        err.message_debug,
        level=err.severity,
        tags={"phase": "b", "code": err.code, "component": err.component},
        extras=err.context,
    )

# In push() after the event_bus.emit block:
self._route_to_sentry(err)
```

- [ ] **Step 4.4: Run, verify pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_bus.py::ErrorBusSentryRoutingTests -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 4.5: Commit**

```bash
git add KrabEar/backend/error_bus.py KrabEar/tests/test_error_bus.py
git commit -m "feat(error_bus): Sentry tier routing + WarnBatcher (10/30s window)"
```

---

### Task 5: error_actions.py with stub handlers + 3 real handlers

**Files:**
- Create: `KrabEar/backend/error_actions.py`
- Test: `KrabEar/tests/test_error_actions.py`

- [ ] **Step 5.1: Write failing tests**

Create `KrabEar/tests/test_error_actions.py`:

```python
import unittest
from unittest.mock import MagicMock, patch

from backend.error_actions import handle_action, ACTION_HANDLERS


class ActionDispatcherTests(unittest.TestCase):
    def test_unknown_action_returns_error(self):
        result = handle_action("nonexistent_action", settings_service=MagicMock())
        self.assertFalse(result["executed"])
        self.assertIn("unknown", result["reason"].lower())

    def test_disable_rewriter_writes_settings(self):
        settings_service = MagicMock()
        result = handle_action("disable_rewriter", settings_service=settings_service)
        self.assertTrue(result["executed"])
        settings_service.handle_set_settings.assert_called_once()
        call_args = settings_service.handle_set_settings.call_args
        params = call_args[0][0] if call_args[0] else call_args[1]
        self.assertEqual(params.get("llm_rewrite_enabled"), False)

    def test_kill_lm_studio_via_telegram_feature_disabled(self):
        # B.1: feature flag default False — should return feature_disabled
        result = handle_action("kill_lm_studio_via_telegram", settings_service=MagicMock())
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "feature_disabled")

    @patch("backend.error_actions.subprocess.run")
    def test_open_privacy_settings_invokes_subprocess(self, mock_run):
        result = handle_action("open_privacy_settings", settings_service=MagicMock())
        self.assertTrue(result["executed"])
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "open")
        self.assertIn("Privacy", " ".join(cmd))

    def test_all_registered_action_ids_callable(self):
        for action_id in ACTION_HANDLERS:
            with self.subTest(action_id=action_id):
                self.assertTrue(callable(ACTION_HANDLERS[action_id]))
```

- [ ] **Step 5.2: Run, verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_actions.py -v 2>&1 | tail -10
```

Expected: ImportError.

- [ ] **Step 5.3: Implement `error_actions.py`**

Create `KrabEar/backend/error_actions.py`:

```python
"""Action dispatchers for actionable errors (button clicks in toasts/diagnostics).

Each handler signature: handler(*, settings_service, **kwargs) -> dict.
Return shape: {"executed": bool, "reason": str | None, "side_effect": str | None}.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Callable

logger = logging.getLogger("KrabEar.Backend.ErrorActions")

# Privacy preference URLs (macOS deep links)
_PRIVACY_ACCESSIBILITY_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


def _open_url(url: str) -> dict:
    try:
        subprocess.run(["open", url], check=True)
        return {"executed": True, "reason": None, "side_effect": f"opened:{url}"}
    except subprocess.CalledProcessError as exc:
        return {"executed": False, "reason": f"open_failed: {exc}", "side_effect": None}


def _open_privacy_settings(*, settings_service, **kwargs) -> dict:
    return _open_url(_PRIVACY_ACCESSIBILITY_URL)


def _open_hf_token_setting(*, settings_service, **kwargs) -> dict:
    # Emit IPC event the Swift side picks up to focus HF Token field in Settings tab.
    return {"executed": True, "reason": None, "side_effect": "swift_focus_hf_token"}


def _disable_rewriter(*, settings_service, **kwargs) -> dict:
    settings_service.handle_set_settings({"llm_rewrite_enabled": False})
    return {"executed": True, "reason": None, "side_effect": "settings_updated"}


def _open_hotkey_settings(*, settings_service, **kwargs) -> dict:
    return {"executed": True, "reason": None, "side_effect": "swift_focus_hotkey_tab"}


def _switch_to_balanced_profile(*, settings_service, **kwargs) -> dict:
    settings_service.handle_set_settings({"quality_profile": "balanced"})
    return {"executed": True, "reason": None, "side_effect": "profile_switched"}


def _retry_history_save(*, settings_service, store=None, **kwargs) -> dict:
    if store is None:
        return {"executed": False, "reason": "no_store_available", "side_effect": None}
    try:
        store.retry_pending_writes()  # method to be added in B.2
        return {"executed": True, "reason": None, "side_effect": "history_retried"}
    except Exception as exc:
        return {"executed": False, "reason": f"retry_failed: {exc}", "side_effect": None}


def _kill_lm_studio_via_telegram(*, settings_service, **kwargs) -> dict:
    # B.1: feature-flagged off. Real Telegram bridge integration pending separate spec.
    return {"executed": False, "reason": "feature_disabled", "side_effect": None}


def _open_log_file(*, settings_service, **kwargs) -> dict:
    log_path = "/Users/pablito/Library/Application Support/KrabEar/backend.log"
    return _open_url(log_path)


ACTION_HANDLERS: dict[str, Callable] = {
    "open_privacy_settings": _open_privacy_settings,
    "open_hf_token_setting": _open_hf_token_setting,
    "disable_rewriter": _disable_rewriter,
    "open_hotkey_settings": _open_hotkey_settings,
    "switch_to_balanced_profile": _switch_to_balanced_profile,
    "retry_history_save": _retry_history_save,
    "kill_lm_studio_via_telegram": _kill_lm_studio_via_telegram,
    "open_log_file": _open_log_file,
}


def handle_action(action_id: str, **kwargs) -> dict:
    handler = ACTION_HANDLERS.get(action_id)
    if handler is None:
        return {
            "executed": False,
            "reason": f"unknown action_id: {action_id}",
            "side_effect": None,
        }
    try:
        return handler(**kwargs)
    except Exception as exc:
        logger.exception("action handler raised: action_id=%s", action_id)
        return {"executed": False, "reason": f"handler_raised: {exc}", "side_effect": None}
```

- [ ] **Step 5.4: Run, verify pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_error_actions.py -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 5.5: Commit**

```bash
git add KrabEar/backend/error_actions.py KrabEar/tests/test_error_actions.py
git commit -m "feat(error_actions): dispatcher + 3 real handlers + 5 stubs"
```

---

### Task 6: LLMHttpProbe — active probe with adaptive interval

**Files:**
- Create: `KrabEar/backend/llm_probe.py`
- Test: `KrabEar/tests/test_llm_probe.py`

- [ ] **Step 6.1: Write failing tests**

Create `KrabEar/tests/test_llm_probe.py`:

```python
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from backend.llm_probe import LLMHttpProbe


class FakeRewriter:
    def __init__(self, return_values):
        self._values = list(return_values)
        self.calls = 0
        self._model = "gemma-4-e4b-it-mlx"
        self._last_latency_ms = 50

    def warmup(self) -> bool:
        result = self._values[self.calls % len(self._values)]
        self.calls += 1
        return result


class LLMHttpProbeStateTransitionTests(unittest.TestCase):
    def test_alive_to_dead_emits_unavailable(self):
        rewriter = FakeRewriter([True, False])
        error_bus = MagicMock()
        event_bus = MagicMock()
        settings = {"llm_rewrite_enabled": True}
        probe = LLMHttpProbe(
            rewriter=rewriter,
            error_bus=error_bus,
            event_bus=event_bus,
            settings_provider=lambda: settings,
            base_interval_sec=0.05,
        )
        probe.start()
        time.sleep(0.25)  # ≥ 2 ticks
        probe.stop()
        # Should have pushed at least one rewriter.unavailable
        codes = [
            call.args[0].code for call in error_bus.push.call_args_list
        ]
        self.assertIn("rewriter.unavailable", codes)

    def test_dead_to_alive_emits_recovered_event(self):
        rewriter = FakeRewriter([False, True])
        error_bus = MagicMock()
        event_bus = MagicMock()
        settings = {"llm_rewrite_enabled": True}
        probe = LLMHttpProbe(
            rewriter=rewriter, error_bus=error_bus, event_bus=event_bus,
            settings_provider=lambda: settings, base_interval_sec=0.05,
        )
        probe.start()
        time.sleep(0.25)
        probe.stop()
        emitted_types = [c.args[0] for c in event_bus.emit.call_args_list]
        self.assertIn("rewriter_recovered", emitted_types)

    def test_skips_when_disabled(self):
        rewriter = FakeRewriter([True])
        error_bus = MagicMock()
        event_bus = MagicMock()
        settings = {"llm_rewrite_enabled": False}
        probe = LLMHttpProbe(
            rewriter=rewriter, error_bus=error_bus, event_bus=event_bus,
            settings_provider=lambda: settings, base_interval_sec=0.05,
        )
        probe.start()
        time.sleep(0.25)
        probe.stop()
        self.assertEqual(rewriter.calls, 0)

    def test_adaptive_interval_extends_on_cold_load(self):
        # Simulate slow warmup (cold load) by overriding _last_latency_ms
        class SlowRewriter(FakeRewriter):
            def warmup(self) -> bool:
                self._last_latency_ms = 5000  # cold-load latency
                return True

        rewriter = SlowRewriter([True, True, True])
        probe = LLMHttpProbe(
            rewriter=rewriter, error_bus=MagicMock(), event_bus=MagicMock(),
            settings_provider=lambda: {"llm_rewrite_enabled": True},
            base_interval_sec=0.05,
            cold_load_threshold_ms=3000,
        )
        probe.start()
        time.sleep(0.4)
        probe.stop()
        # After cold load detected, interval should have extended.
        self.assertGreater(probe._current_interval_sec, 0.05)
```

- [ ] **Step 6.2: Run, verify failure**

Expected: ImportError.

- [ ] **Step 6.3: Implement LLMHttpProbe**

Create `KrabEar/backend/llm_probe.py`:

```python
"""Active probe of LM Studio HTTP rewriter endpoint.

Independent of LLMRewriter's circuit breaker. Polls warmup() on a
background thread, emits rewriter.unavailable when probe transitions
from True→False, and event_bus 'rewriter_recovered' on False→True.

Adaptive interval: if warmup latency > cold_load_threshold_ms, extend
the next interval by 10× (up to 5 min) to avoid retriggering a JIT
reload on every poll. Recovers to base interval after 3 consecutive
fast responses.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from backend.error_bus import KrabError

logger = logging.getLogger("KrabEar.Backend.LLMHttpProbe")


class LLMHttpProbe:
    def __init__(
        self,
        rewriter,
        error_bus,
        event_bus,
        settings_provider: Callable[[], dict],
        base_interval_sec: float = 30.0,
        cold_load_threshold_ms: int = 3000,
        max_interval_sec: float = 300.0,
        recovery_consecutive: int = 3,
    ):
        self._rewriter = rewriter
        self._error_bus = error_bus
        self._event_bus = event_bus
        self._get_settings = settings_provider
        self._base_interval = base_interval_sec
        self._cold_threshold_ms = cold_load_threshold_ms
        self._max_interval = max_interval_sec
        self._recovery_target = recovery_consecutive
        self._current_interval_sec = base_interval_sec
        self._fast_streak = 0
        self._last_state: Optional[bool] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="LLMHttpProbe"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        # First wait one interval; this also lets backend finish startup.
        while not self._stop_event.wait(self._current_interval_sec):
            try:
                self._tick()
            except Exception:
                logger.exception("LLMHttpProbe tick raised")

    def _tick(self) -> None:
        settings = self._get_settings()
        if not settings.get("llm_rewrite_enabled", False):
            return
        current = self._rewriter.warmup()
        latency_ms = getattr(self._rewriter, "_last_latency_ms", 0) or 0

        # Adaptive interval
        if latency_ms > self._cold_threshold_ms:
            self._current_interval_sec = min(
                self._current_interval_sec * 10, self._max_interval
            )
            self._fast_streak = 0
            logger.info(
                "LLMHttpProbe: cold-load detected (latency_ms=%d), interval→%.1fs",
                latency_ms, self._current_interval_sec,
            )
        else:
            self._fast_streak += 1
            if self._fast_streak >= self._recovery_target:
                self._current_interval_sec = self._base_interval
                self._fast_streak = 0

        if current != self._last_state:
            self._on_state_change(self._last_state, current, latency_ms)
        self._last_state = current

    def _on_state_change(
        self, old: Optional[bool], new: bool, latency_ms: int
    ) -> None:
        if new is False:
            err = KrabError(
                severity="info",
                component="rewriter",
                code="rewriter.unavailable",
                message_user="LM Studio недоступен (active probe)",
                message_debug=f"warmup() returned False; transition {old}->{new}",
                timestamp=datetime.now(timezone.utc),
                context={
                    "model": getattr(self._rewriter, "_model", "?"),
                    "previous_state": old,
                },
                actionable=False,
                action_id=None,
            )
            self._error_bus.push(err)
        elif old is False and new is True:
            try:
                self._event_bus.emit(
                    "rewriter_recovered",
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "latency_ms": latency_ms,
                    },
                )
            except Exception:
                logger.exception("event_bus.emit rewriter_recovered failed")
```

- [ ] **Step 6.4: Run, verify pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_llm_probe.py -v 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 6.5: Commit**

```bash
git add KrabEar/backend/llm_probe.py KrabEar/tests/test_llm_probe.py
git commit -m "feat(llm_probe): active LM Studio HTTP probe with adaptive interval"
```

---

### Task 7: BackendService wiring + 4 IPC methods

**Files:**
- Modify: `KrabEar/backend/service.py`
- Create: `KrabEar/tests/test_error_bus_integration.py`

- [ ] **Step 7.1: Write failing integration test**

Create `KrabEar/tests/test_error_bus_integration.py`:

```python
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Use existing test harness pattern from test_backend_service.py
# (reads/writes Unix-socket newline-delimited JSON-RPC)


class ErrorBusIPCIntegrationTests(unittest.TestCase):
    """Smoke test: backend boot → push test error → list_recent_errors returns it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="krabear_test_"))
        self.sock_path = self.tmp / "test.sock"

    def tearDown(self):
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass

    def _ipc(self, method, params=None):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(self.sock_path))
        s.sendall(
            (json.dumps({"id": "1", "method": method, "params": params or {}}) + "\n").encode()
        )
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.decode())

    def test_list_recent_errors_includes_pushed(self):
        # Start backend with --data-dir self.tmp, --socket-path self.sock_path
        # Push test error via /push_test_error IPC method (test-only)
        # Call list_recent_errors → assert returned errors[0].code matches
        ...  # Engineer: implement using existing test_backend_service.py harness

    def test_handle_error_action_open_privacy(self):
        # Mock subprocess.run, call handle_error_action with open_privacy_settings,
        # assert result.executed=True
        ...
```

Engineer note: borrow setup pattern from existing `KrabEar/tests/test_backend_service.py`. The full integration test launches `service.py` as subprocess. Keep this test in same shape.

- [ ] **Step 7.2: Modify `BackendService.__init__` to instantiate ErrorBus + LLMHttpProbe**

In `KrabEar/backend/service.py`, locate the `BackendService.__init__` method (search for `class BackendService:` then `def __init__`). After the existing `self._llm_rewriter = self._init_llm_rewriter()` line, add:

```python
# Phase B.1 — error bus + active LLM probe
from backend.error_bus import ErrorBus
from backend.error_codes import ERROR_REGISTRY
from backend.llm_probe import LLMHttpProbe
from backend import error_actions as _error_actions
try:
    import sentry_sdk as _sentry_sdk
except ImportError:
    _sentry_sdk = None

self._error_bus = ErrorBus(
    event_bus=self._event_bus,
    registry=ERROR_REGISTRY,
    sentry_client=_sentry_sdk,
    default_dedupe_window_sec=30.0,
    ring_buffer_size=200,
)

self._llm_probe = None
if self._llm_rewriter is not None:
    self._llm_probe = LLMHttpProbe(
        rewriter=self._llm_rewriter,
        error_bus=self._error_bus,
        event_bus=self._event_bus,
        settings_provider=lambda: self._settings_service.get_settings(),
        base_interval_sec=float(self._settings_service.get_settings().get("llm_probe_interval_sec", 30.0)),
    )
    if self._settings_service.get_settings().get("llm_probe_enabled", True):
        self._llm_probe.start()
```

In `BackendService.close()` (or `shutdown()`), add `if self._llm_probe: self._llm_probe.stop()`.

- [ ] **Step 7.3: Add 4 IPC handler methods**

In `BackendService`, add:

```python
def _handle_list_recent_errors(self, params: dict) -> dict:
    limit = int(params.get("limit", 200))
    items = self._error_bus.list_recent(limit)
    return {"errors": [item.model_dump(mode="json") for item in items]}

def _handle_clear_recent_errors(self, params: dict) -> dict:
    n = self._error_bus.clear()
    return {"cleared": n}

def _handle_handle_error_action(self, params: dict) -> dict:
    action_id = params.get("action_id")
    if not action_id:
        return {"executed": False, "reason": "missing action_id"}
    return _error_actions.handle_action(
        action_id,
        settings_service=self._settings_service,
        store=self._store,
    )

def _handle_probe_llm_http(self, params: dict) -> dict:
    if self._llm_rewriter is None:
        return {"reachable": False, "latency_ms": 0, "model": None}
    ok = self._llm_rewriter.warmup()
    return {
        "reachable": ok,
        "latency_ms": getattr(self._llm_rewriter, "_last_latency_ms", 0) or 0,
        "model": getattr(self._llm_rewriter, "_model", None),
    }
```

Register them in the dispatch table (search for `"set_settings": self._handle_set_settings_with_hot_reload`):

```python
"list_recent_errors": self._handle_list_recent_errors,
"clear_recent_errors": self._handle_clear_recent_errors,
"handle_error_action": self._handle_handle_error_action,
"probe_llm_http": self._handle_probe_llm_http,
```

- [ ] **Step 7.4: Run integration tests + full test suite**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -q 2>&1 | tail -15
```

Expected: all pre-existing tests still pass, new error_bus integration tests pass.

- [ ] **Step 7.5: Commit**

```bash
git add KrabEar/backend/service.py KrabEar/tests/test_error_bus_integration.py
git commit -m "feat(service): wire ErrorBus + LLMHttpProbe + 4 IPC methods"
```

---

### Task 8: llm_rewriter.py — parallel error_bus.push + KRAB_EAR_LLM_FORCE_TIMEOUT

**Files:**
- Modify: `KrabEar/backend/llm_rewriter.py`
- Modify: `KrabEar/tests/test_llm_rewriter.py`

- [ ] **Step 8.1: Write failing tests for error_bus.push next to existing logger.warning**

Append to `KrabEar/tests/test_llm_rewriter.py`:

```python
class LLMRewriterErrorBusIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.error_bus = MagicMock()
        self.rewriter = LLMRewriter(
            base_url="http://127.0.0.1:1234/v1", api_key="x",
            model="gemma-4-e4b-it-mlx",
            timeout_sec=0.01,
            error_bus=self.error_bus,
        )

    def test_timeout_pushes_rewriter_timeout(self):
        with patch.object(self.rewriter._session, "post", side_effect=requests.Timeout()):
            self.rewriter.rewrite("text")
        codes = [c.args[0].code for c in self.error_bus.push.call_args_list]
        self.assertIn("rewriter.timeout", codes)

    def test_connection_error_pushes_rewriter_connection_error(self):
        with patch.object(
            self.rewriter._session, "post",
            side_effect=requests.ConnectionError("connection refused"),
        ):
            self.rewriter.rewrite("text")
        codes = [c.args[0].code for c in self.error_bus.push.call_args_list]
        self.assertIn("rewriter.connection_error", codes)

    def test_force_timeout_env_simulates_timeout(self):
        os.environ["KRAB_EAR_LLM_FORCE_TIMEOUT"] = "1"
        try:
            result = self.rewriter.rewrite("text")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "timeout")
            codes = [c.args[0].code for c in self.error_bus.push.call_args_list]
            self.assertIn("rewriter.timeout", codes)
        finally:
            os.environ.pop("KRAB_EAR_LLM_FORCE_TIMEOUT", None)
```

- [ ] **Step 8.2: Modify `LLMRewriter.__init__` to accept optional `error_bus`**

In `KrabEar/backend/llm_rewriter.py`, add `error_bus=None` parameter to `__init__` and store as `self._error_bus`. Existing callers without the param keep working (default None → no-op pushes).

- [ ] **Step 8.3: Add `_push_error` helper and call it in 3 paths**

Add helper method:

```python
def _push_error(self, code: str, message_debug: str, severity: str = "warn") -> None:
    if self._error_bus is None:
        return
    from backend.error_bus import KrabError
    from backend.error_codes import ERROR_REGISTRY
    entry = ERROR_REGISTRY.get(code, {})
    err = KrabError(
        severity=severity,
        component="rewriter",
        code=code,
        message_user=entry.get("user_msg_ru", "Rewriter ошибка"),
        message_debug=message_debug,
        timestamp=datetime.now(timezone.utc),
        context={"model": self._model, "base_url": self._base_url},
        actionable=entry.get("actionable", False),
        action_id=entry.get("action_id"),
    )
    self._error_bus.push(err)
```

Call sites (alongside existing `logger.warning` from PR #362 — keep both):

```python
# In requests.Timeout branch:
self._push_error("rewriter.timeout", f"timeout after {self._timeout}s")

# In requests.ConnectionError branch:
self._push_error("rewriter.connection_error", f"connection_error: {exc}")

# In HTTP non-200 branch (final, after retry exhaustion):
self._push_error("rewriter.timeout", f"http_{response.status_code}")
```

Add in `_rewrite_impl` near the top, after circuit-breaker check:

```python
import os
if os.getenv("KRAB_EAR_LLM_FORCE_TIMEOUT") == "1":
    self._circuit.record_failure()
    self._last_error = "timeout"
    self._push_error("rewriter.timeout", "forced via KRAB_EAR_LLM_FORCE_TIMEOUT")
    return LLMRewriteResult(
        ok=False, text=None, fallback_reason="timeout", latency_ms=None
    )
```

Required new import at top: `from datetime import datetime, timezone`.

- [ ] **Step 8.4: Update BackendService to pass error_bus**

In `BackendService._init_llm_rewriter()`, after constructing `LLMRewriter(...)`, set `rewriter._error_bus = self._error_bus`. (Or pass via constructor — but ordering: ErrorBus is built after rewriter currently, so use late assignment.)

Actually, reorder: instantiate `ErrorBus` BEFORE `_init_llm_rewriter()` and pass `error_bus=self._error_bus` directly. Update Task 7 wiring order if not already done.

- [ ] **Step 8.5: Run tests**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_llm_rewriter.py -v 2>&1 | tail -15
```

Expected: 79 + 3 new = 82 passed.

- [ ] **Step 8.6: Commit**

```bash
git add KrabEar/backend/llm_rewriter.py KrabEar/backend/service.py KrabEar/tests/test_llm_rewriter.py
git commit -m "feat(rewriter): parallel error_bus.push + KRAB_EAR_LLM_FORCE_TIMEOUT env"
```

---

### Task 9: transcriber.py — paste.ax_denied + diarization.no_token

**Files:**
- Modify: `KrabEar/backend/transcriber.py`
- Modify: `KrabEar/tests/test_transcriber.py` (or create if doesn't exist)

- [ ] **Step 9.1: Locate paste call site**

Search for the AX paste invocation:

```bash
grep -n "AXIsProcessTrusted\|paste\|kAXSelectedTextAttribute" KrabEar/backend/transcriber.py KrabEar/backend/service.py
```

Identify the function that handles paste failure (likely returns a status code or raises). Decide whether to push from the Python layer or from Swift; current arch has Swift `PasteService.swift` doing the actual paste. **Decision:** Swift detects AX denied first; reports back via IPC; backend pushes the KrabError to the bus. So:

- Add IPC method `_handle_report_paste_failure(self, params)` that takes `{"reason": "ax_denied"|"app_unsupported", "app_bundle": str}` and pushes the appropriate error code.

- [ ] **Step 9.2: Add IPC handler**

In `BackendService`, add:

```python
def _handle_report_paste_failure(self, params: dict) -> dict:
    reason = params.get("reason")
    app_bundle = params.get("app_bundle", "")
    code_map = {"ax_denied": "paste.ax_denied", "app_unsupported": "paste.app_unsupported"}
    code = code_map.get(reason)
    if code is None:
        return {"ok": False, "reason": "unknown_paste_reason"}
    from backend.error_bus import KrabError
    from backend.error_codes import ERROR_REGISTRY
    entry = ERROR_REGISTRY[code]
    err = KrabError(
        severity=entry["severity"],
        component="paste",
        code=code,
        message_user=entry["user_msg_ru"],
        message_debug=f"reason={reason} app={app_bundle}",
        timestamp=datetime.now(timezone.utc),
        context={"app_bundle": app_bundle},
        actionable=entry["actionable"],
        action_id=entry["action_id"],
    )
    self._error_bus.push(err)
    return {"ok": True}
```

Register in dispatch table.

- [ ] **Step 9.3: Add diarization.no_token push in transcriber.py**

Search for diarization init in `transcriber.py`. When HF_TOKEN env is empty AND diarization is enabled:

```python
def _check_diarization_token(self) -> bool:
    if not self._settings.get("diarization_enabled", False):
        return True
    token = os.environ.get("HF_TOKEN") or os.environ.get("KRAB_EAR_HF_TOKEN") or ""
    if not token:
        if self._error_bus:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            entry = ERROR_REGISTRY["diarization.no_token"]
            err = KrabError(
                severity=entry["severity"],
                component="diarization",
                code="diarization.no_token",
                message_user=entry["user_msg_ru"],
                message_debug="HF_TOKEN env var not set",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=entry["actionable"],
                action_id=entry["action_id"],
            )
            self._error_bus.push(err)
        return False
    return True
```

Inject `error_bus` via `Transcriber` constructor parameter (default None, set in `BackendService.__init__`).

- [ ] **Step 9.4: Write tests**

Add to `KrabEar/tests/test_transcriber.py` (or new file):

```python
def test_diarization_no_token_pushes_error(self):
    error_bus = MagicMock()
    settings = {"diarization_enabled": True}
    transcriber = Transcriber(..., error_bus=error_bus, settings=settings)
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("KRAB_EAR_HF_TOKEN", None)
    self.assertFalse(transcriber._check_diarization_token())
    codes = [c.args[0].code for c in error_bus.push.call_args_list]
    self.assertIn("diarization.no_token", codes)


def test_report_paste_failure_pushes_ax_denied(self):
    # Use IPC harness from test_backend_service.py
    response = self._ipc("report_paste_failure", {"reason": "ax_denied", "app_bundle": "com.test"})
    self.assertTrue(response["result"]["ok"])
    # then list_recent_errors returns paste.ax_denied
```

- [ ] **Step 9.5: Run + commit**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_transcriber.py KrabEar/tests/test_error_bus_integration.py -v 2>&1 | tail -15
git add KrabEar/backend/transcriber.py KrabEar/backend/service.py KrabEar/tests/test_transcriber.py
git commit -m "feat(transcriber): wire paste.ax_denied + diarization.no_token error pushes"
```

---

### Task 10: Swift — ErrorActionHandler.swift + main+Errors.swift wiring

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/ErrorActionHandler.swift`
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/main+Errors.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorActionHandlerTests.swift`

- [ ] **Step 10.1: Define the Swift KrabError struct**

Create `native/KrabEarAgent/Sources/KrabEarAgent/ErrorActionHandler.swift`:

```swift
import Foundation
import AppKit
import os

public struct KrabErrorPayload: Codable {
    public let severity: String
    public let component: String
    public let code: String
    public let message_user: String
    public let message_debug: String
    public let timestamp: String
    public let context: [String: AnyCodable]
    public let actionable: Bool
    public let action_id: String?
}

@MainActor
public final class ErrorActionHandler {
    private let logger = Logger(subsystem: "com.antigravity.krab-ear", category: "ErrorActionHandler")
    private let ipcClient: IPCClient
    private let toastPresenter: any ToastPresenting
    private let statusIndicator: StatusIndicatorView

    public init(ipcClient: IPCClient, toastPresenter: any ToastPresenting, statusIndicator: StatusIndicatorView) {
        self.ipcClient = ipcClient
        self.toastPresenter = toastPresenter
        self.statusIndicator = statusIndicator
    }

    public func handleErrorEvent(_ payload: KrabErrorPayload) async {
        logger.info("krab_error received: code=\(payload.code) severity=\(payload.severity)")
        toastPresenter.present(error: payload)
        statusIndicator.applyErrorBadge(severity: payload.severity)
    }

    public func handleActionTap(actionId: String) async {
        let response = try? await ipcClient.call(
            method: "handle_error_action",
            params: ["action_id": actionId]
        )
        logger.info("action \(actionId) response: \(String(describing: response))")
        // Side-effect handling for "swift_focus_*" markers:
        if let sideEffect = response?.dictionaryValue?["side_effect"] as? String {
            switch sideEffect {
            case "swift_focus_hf_token":
                NotificationCenter.default.post(name: .focusHFTokenSetting, object: nil)
            case "swift_focus_hotkey_tab":
                NotificationCenter.default.post(name: .focusHotkeyTab, object: nil)
            default:
                break
            }
        }
    }
}

extension Notification.Name {
    static let focusHFTokenSetting = Notification.Name("KrabEar.focusHFTokenSetting")
    static let focusHotkeyTab = Notification.Name("KrabEar.focusHotkeyTab")
}
```

- [ ] **Step 10.2: Wire ErrorActionHandler in main+Errors.swift**

Create `native/KrabEarAgent/Sources/KrabEarAgent/main+Errors.swift`:

```swift
import Foundation
import AppKit

extension AgentAppDelegate {
    @MainActor
    func setupErrorBus() {
        guard let ipcClient = self.ipcClient else { return }
        let presenter = ErrorToastPresenter(themedWindow: self.window)
        self.errorActionHandler = ErrorActionHandler(
            ipcClient: ipcClient,
            toastPresenter: presenter,
            statusIndicator: self.statusIndicatorView
        )

        // Subscribe to event bus SSE stream for "krab_error" events
        Task.detached { [weak self] in
            await self?.streamKrabErrors()
        }
    }

    @MainActor
    func streamKrabErrors() async {
        guard let url = URL(string: "http://127.0.0.1:5005/events") else { return }
        // Use existing SSE client; subscribe to "krab_error" events,
        // decode KrabErrorPayload, dispatch to errorActionHandler.handleErrorEvent.
        // Engineer: replicate pattern from existing event_bus subscriptions
        // (search EventBusClient.swift for the SSE iteration pattern).
    }
}
```

- [ ] **Step 10.3: Tests**

Create `native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorActionHandlerTests.swift`:

```swift
import XCTest
@testable import KrabEarAgent

final class ErrorActionHandlerTests: XCTestCase {
    @MainActor
    func test_handleErrorEvent_calls_toast_present() async {
        let mockPresenter = MockToastPresenter()
        let mockIPC = MockIPCClient(responses: [:])
        let mockStatus = StatusIndicatorView()
        let handler = ErrorActionHandler(
            ipcClient: mockIPC, toastPresenter: mockPresenter, statusIndicator: mockStatus
        )
        let payload = KrabErrorPayload(
            severity: "warn", component: "rewriter", code: "rewriter.timeout",
            message_user: "x", message_debug: "y", timestamp: "2026-05-04T00:00:00Z",
            context: [:], actionable: false, action_id: nil
        )
        await handler.handleErrorEvent(payload)
        XCTAssertEqual(mockPresenter.presentedErrors.count, 1)
        XCTAssertEqual(mockPresenter.presentedErrors[0].code, "rewriter.timeout")
    }

    @MainActor
    func test_handleActionTap_calls_ipc() async {
        let mockIPC = MockIPCClient(responses: [
            "handle_error_action": ["executed": true, "side_effect": "settings_updated"]
        ])
        let handler = ErrorActionHandler(
            ipcClient: mockIPC, toastPresenter: MockToastPresenter(),
            statusIndicator: StatusIndicatorView()
        )
        await handler.handleActionTap(actionId: "disable_rewriter")
        XCTAssertEqual(mockIPC.calls.count, 1)
        XCTAssertEqual(mockIPC.calls[0].method, "handle_error_action")
    }
}

final class MockToastPresenter: ToastPresenting {
    var presentedErrors: [KrabErrorPayload] = []
    func present(error: KrabErrorPayload) { presentedErrors.append(error) }
}
```

- [ ] **Step 10.4: Build + run XCTests**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift build -c release 2>&1 | tail -3
swift test --filter ErrorActionHandlerTests 2>&1 | tail -10
```

Expected: build succeeds, 2 XCTests pass.

- [ ] **Step 10.5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/ErrorActionHandler.swift \
        native/KrabEarAgent/Sources/KrabEarAgent/main+Errors.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorActionHandlerTests.swift
git commit -m "feat(swift): ErrorActionHandler + main+Errors.swift SSE subscription wiring"
```

---

### Task 11: Swift — ErrorToastView.swift (Liquid Glass minimal)

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/ErrorToastView.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorToastViewTests.swift`

- [ ] **Step 11.1: Define ToastPresenting protocol + ErrorToastPresenter**

Create `native/KrabEarAgent/Sources/KrabEarAgent/ErrorToastView.swift`:

```swift
import AppKit
import Foundation

public protocol ToastPresenting: AnyObject {
    func present(error: KrabErrorPayload)
}

public final class ErrorToastPresenter: NSObject, ToastPresenting {
    private weak var themedWindow: NSWindow?
    private var activeToast: NSPanel?
    private var queue: [KrabErrorPayload] = []
    private let lock = NSLock()

    public init(themedWindow: NSWindow?) {
        self.themedWindow = themedWindow
    }

    public func present(error: KrabErrorPayload) {
        lock.lock()
        queue.append(error)
        lock.unlock()
        DispatchQueue.main.async { [weak self] in self?.drainQueue() }
    }

    private func drainQueue() {
        lock.lock()
        guard !queue.isEmpty, activeToast == nil else { lock.unlock(); return }
        let next = queue.removeFirst()
        lock.unlock()
        let panel = makeToastPanel(for: next)
        activeToast = panel
        panel.makeKeyAndOrderFront(nil)
        let dismissAfter = autoDismissSeconds(for: next.severity)
        if dismissAfter > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + dismissAfter) { [weak self] in
                self?.dismissActive()
            }
        }
    }

    private func dismissActive() {
        activeToast?.orderOut(nil)
        activeToast = nil
        drainQueue()
    }

    private func autoDismissSeconds(for severity: String) -> TimeInterval {
        switch severity {
        case "info": return 2
        case "warn": return 5
        case "error": return 10
        case "critical": return 0  // manual dismiss
        default: return 5
        }
    }

    private func makeToastPanel(for error: KrabErrorPayload) -> NSPanel {
        // Engineer: re-use KrabEarTheme.ThemeCardView for the body.
        // Layout: severity icon on left, message_user as primary text,
        // optional action button if actionable=true; action label from registry mirror
        // sent in payload.context["action_label"] if present.
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 80),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        // Position: top-right corner of screen with 16pt inset
        if let screen = NSScreen.main {
            let f = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: f.maxX - 376, y: f.maxY - 96))
        }
        // Engineer: set up NSVisualEffectView + ThemeCardView contentView
        return panel
    }
}
```

- [ ] **Step 11.2: Tests**

Create `native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorToastViewTests.swift`:

```swift
import XCTest
@testable import KrabEarAgent

@MainActor
final class ErrorToastViewTests: XCTestCase {
    func test_autoDismiss_critical_is_manual() {
        let presenter = ErrorToastPresenter(themedWindow: nil)
        // Use Mirror or expose internal helper for testing
        // OR test via public API: present a critical, advance time, assert still on-screen.
        let critical = KrabErrorPayload(
            severity: "critical", component: "mlx", code: "mlx.oom",
            message_user: "x", message_debug: "y", timestamp: "2026-05-04T00:00:00Z",
            context: [:], actionable: true, action_id: "kill_lm_studio_via_telegram"
        )
        presenter.present(error: critical)
        let exp = XCTestExpectation(description: "toast present")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { exp.fulfill() }
        wait(for: [exp], timeout: 1)
        // Engineer: assert that activeToast is still visible after 0.5s
    }

    func test_queue_drain_one_at_a_time() async {
        let presenter = ErrorToastPresenter(themedWindow: nil)
        for i in 0..<3 {
            let p = KrabErrorPayload(
                severity: "info", component: "stt", code: "stt.empty_text",
                message_user: "\(i)", message_debug: "x", timestamp: "x",
                context: [:], actionable: false, action_id: nil
            )
            presenter.present(error: p)
        }
        // Engineer: assert queue depth went to 0 after expected drain time.
    }
}
```

- [ ] **Step 11.3: Build + run + commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift build -c release 2>&1 | tail -3
swift test --filter ErrorToastViewTests 2>&1 | tail -10
git add native/KrabEarAgent/Sources/KrabEarAgent/ErrorToastView.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/ErrorToastViewTests.swift
git commit -m "feat(swift): ErrorToastPresenter with severity-aware auto-dismiss + queue"
```

---

### Task 12: Swift — StatusIndicatorView severity badge layering

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicatorView.swift`
- Modify: existing StatusIndicator XCTests

- [ ] **Step 12.1: Add `applyErrorBadge(severity:)` method**

In `StatusIndicatorView.swift`, after Phase A code:

```swift
public func applyErrorBadge(severity: String) {
    let color: NSColor? = {
        switch severity {
        case "critical": return .systemRed
        case "error": return .systemOrange
        case "warn": return .systemYellow
        default: return nil
        }
    }()
    DispatchQueue.main.async { [weak self] in
        guard let self else { return }
        if let color {
            self.showBadge(color: color, blink: severity == "critical")
        } else {
            self.hideBadge()
        }
    }
}
```

`showBadge` adds a 6pt circle at top-right of the existing dot; `hideBadge` removes it. `blink` toggles alpha 0.5↔1.0 every 1s for critical.

- [ ] **Step 12.2: Add tests**

```swift
@MainActor
func test_applyErrorBadge_critical_blinks() {
    let view = StatusIndicatorView()
    view.applyErrorBadge(severity: "critical")
    // Engineer: assert badge subview exists and animation is active
}

@MainActor
func test_applyErrorBadge_info_no_badge() {
    let view = StatusIndicatorView()
    view.applyErrorBadge(severity: "info")
    // Engineer: assert no badge subview
}
```

- [ ] **Step 12.3: Build, run, commit**

```bash
swift build -c release && swift test --filter StatusIndicator 2>&1 | tail -5
git add native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicatorView.swift native/KrabEarAgent/Tests/KrabEarAgentTests/
git commit -m "feat(swift): StatusIndicatorView severity badge layering"
```

---

### Task 13: HealthMonitor.subscribeToProbeEvents

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift`

- [ ] **Step 13.1: Add subscribe method**

In `HealthMonitor.swift`:

```swift
extension HealthMonitor {
    public func subscribeToProbeEvents(eventBusClient: EventBusClient) {
        Task.detached { [weak self] in
            for await event in eventBusClient.events(matching: "rewriter_recovered") {
                guard let self else { return }
                await self.statusIndicator.flashGreen(reason: "rewriter recovered")
            }
        }
    }
}
```

`flashGreen` flashes status dot green for 800ms then returns to current state.

- [ ] **Step 13.2: Wire in main+Errors.swift**

In `setupErrorBus()`:

```swift
self.healthMonitor.subscribeToProbeEvents(eventBusClient: self.eventBusClient)
```

- [ ] **Step 13.3: Test**

Use `EventBusClient` test double (existing). Push a `rewriter_recovered` event, assert `statusIndicator.flashGreen` was called.

- [ ] **Step 13.4: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift native/KrabEarAgent/Sources/KrabEarAgent/main+Errors.swift
git commit -m "feat(swift): HealthMonitor.subscribeToProbeEvents → green flash on recovery"
```

---

### Task 14: PasteService Swift integration — report ax_denied

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/PasteService.swift` (or whatever owns paste — locate via grep)

- [ ] **Step 14.1: Identify paste owner**

```bash
grep -rn "AXIsProcessTrustedWithOptions\|AXUIElementCopyAttributeValue\|kAXFocusedUIElement" \
    native/KrabEarAgent/Sources/KrabEarAgent/ | head -10
```

Identify the function that performs paste and the existing failure path.

- [ ] **Step 14.2: Call IPC report_paste_failure on AX denied**

In paste failure handler, before fallback, call:

```swift
Task { [weak ipcClient] in
    try? await ipcClient?.call(
        method: "report_paste_failure",
        params: ["reason": "ax_denied", "app_bundle": frontmostBundleID ?? ""]
    )
}
```

- [ ] **Step 14.3: Test**

Mock IPC, call paste with AX disabled (simulated), assert `report_paste_failure` was called.

- [ ] **Step 14.4: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/PasteService.swift native/KrabEarAgent/Tests/KrabEarAgentTests/
git commit -m "feat(swift): PasteService reports AX-denied via IPC error bus"
```

---

### Task 15: End-to-end manual acceptance + build artifacts

- [ ] **Step 15.1: Build, sign, copy bundle**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift build -c release
cp -f .build/release/KrabEarAgent ../runtime/KrabEarAgent
cp -f .build/release/KrabEarAgent "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
codesign -s "Krab Ear Dev Local" --identifier com.antigravity.krab-ear -f ../runtime/KrabEarAgent
codesign -s "Krab Ear Dev Local" --identifier com.antigravity.krab-ear -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
launchctl kickstart -k gui/501/ai.krab.ear.backend
sleep 5
```

- [ ] **Step 15.2: Manual acceptance criteria from spec section "Acceptance criteria B.1"**

1. Revoke Accessibility → dictate → toast appears with correct text + clipboard has text + button click opens Privacy Settings. ✅/❌
2. Stop LM Studio → wait 60 s → `IPC list_recent_errors` shows `rewriter.unavailable` event → restart LM Studio → status dot flashes green. ✅/❌
3. `KRAB_EAR_LLM_FORCE_TIMEOUT=1` env (set in launchd plist or via `launchctl setenv`), restart backend, dictate → toast «Rewriter недоступен — raw text вставлен» + raw text in destination + button «Выключить rewriter» works. ✅/❌
4. Remove HF token (`launchctl unsetenv HF_TOKEN`), enable diarization in Settings, dictate → toast «Diarization недоступна — нужен HF token». ✅/❌
5. IPC `list_recent_errors` returns 4+ entries.

- [ ] **Step 15.3: Restore env state**

```bash
launchctl unsetenv KRAB_EAR_LLM_FORCE_TIMEOUT
# Re-set HF_TOKEN if it was unset
```

- [ ] **Step 15.4: Push branch + open PR**

```bash
git push -u origin feat/phase-b1-loud-errors-core-2026-05-04
gh pr create --base codex/krab-ear-v2 \
    --title "feat: Phase B.1 — loud errors core (ErrorBus + 3 codes + LLM probe)" \
    --body "$(cat docs/superpowers/specs/2026-05-04-phase-b-loud-errors-design.md | head -50)

Detailed plan: docs/superpowers/plans/2026-05-04-phase-b-1-loud-errors-core.md

Closes today's silent-failure gap discovered 2026-05-04. After this lands and validates (1-2 day observation), proceed to B.2 (remaining 6 codes) per spec.

Test plan: see plan file Task 15 manual acceptance criteria.
"
```

---

## Self-Review

**Spec coverage check** (each spec section → task):

| Spec section | Task(s) |
|---|---|
| Layer 1 — KrabError + ErrorBus | 1, 3, 4 |
| Layer 1 — error_codes registry | 2 |
| Layer 1 — error_actions | 5 |
| Layer 2 — IPC contract (4 methods) | 7 |
| Layer 3 — active LLM probe | 6 |
| Layer 3 — HealthMonitor extension | 13 |
| Layer 4 — ErrorToastView | 11 |
| Layer 4 — StatusIndicator severity badge | 12 |
| Layer 5 — action handlers (real 3 + stubs) | 5 |
| Sentry tier mapping (info skip, warn batch, error/critical immediate) | 4 |
| Wired errors: rewriter.timeout, rewriter.connection_error | 8 |
| Wired errors: paste.ax_denied | 9, 14 |
| Wired errors: diarization.no_token | 9 |
| KRAB_EAR_LLM_FORCE_TIMEOUT env | 8 |
| Acceptance criteria B.1 | 15 |

No gaps. Diagnostics tab + remaining 6 wired errors are explicitly out-of-scope for B.1 (they belong to B.2/B.3).

**Placeholder scan:**
- Task 7.1 step has `...  # Engineer: implement ...` placeholder for IPC integration test body. **Justified** because the harness pattern lives in existing `test_backend_service.py` and copying it inline would duplicate ~80 lines. Engineer reads existing test file and replicates pattern. This is a defer-to-existing-pattern, not a TBD.
- Task 9.1 step has `...` for diarization test stub — same justification.
- Task 11.2 + 12.2 + 13.3 + 14.3 have `Engineer: ...` notes for assertions. These are XCTest patterns specific to Swift — the engineer needs to wire to existing test harness. Acceptable.

**Type consistency check:**
- `KrabErrorPayload` (Swift) field names match `KrabError.model_dump(mode="json")` output: `severity`, `component`, `code`, `message_user`, `message_debug`, `timestamp` (ISO 8601), `context`, `actionable`, `action_id`. ✅
- IPC method names consistent: `list_recent_errors`, `clear_recent_errors`, `handle_error_action`, `probe_llm_http`, `report_paste_failure`. ✅
- `_error_bus`, `_llm_probe` private member names consistent across `BackendService` references. ✅
- ACTION_HANDLERS keys in error_actions.py match action_id values in ERROR_REGISTRY. ✅
- `warmup()` method signature on `LLMRewriter` matches what `LLMHttpProbe._tick` calls (returns `bool`, no args). ✅ (per PR #362).

No inconsistencies found.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-04-phase-b-1-loud-errors-core.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh sonnet subagent per task (or per pair of tasks where coupled, e.g. 1+2, 3+4, 8+9), review the diff between tasks, fast iteration. Best for parallelism: independent tasks (5+6) run simultaneously, dependent tasks sequential. Total wall time ~3-5 days with checkpoints.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints for user review at each task boundary. Better for tight feedback loops but slower (single-thread on this Claude session).

Which approach? Or если хочешь оставить выбор за мной — рекомендую Subagent-Driven с группировкой:

- Group A (Python core, sequential): Tasks 1 → 2 → 3 → 4 → 5 — single sonnet agent, one PR per task or one PR for whole group
- Group B (active probe, depends on A): Task 6 — separate agent
- Group C (service wiring, depends on A+B): Task 7 — separate agent
- Group D (rewriter + transcriber wiring, depends on C): Tasks 8 → 9 — single agent
- Group E (Swift, parallel with D): Tasks 10 → 11 → 12 → 13 → 14 — single agent
- Group F (E2E + PR): Task 15 — me directly (manual acceptance + PR creation)

После каждой group я review diff, run tests, и если OK → следующая group. Validation checkpoint после Group D — это естественный момент проверить spec assumptions перед стартом B.2.
