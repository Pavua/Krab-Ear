# Wave 797 — service.py Architecture Split Proposal

**Date:** 2026-05-26  
**Author:** Claude Code (Sonnet 4.6)  
**Branch:** `feature/service-py-split-prep-W797`  
**Status:** SPEC ONLY — no code changed

---

## 1. Current State

### File: `KrabEar/backend/service.py`

| Metric | Value |
|--------|-------|
| Total LOC | **2 746** |
| Classes defined | 2 (`BackendService`, `IPCServer`) |
| Module-level functions | 7 (`configure_logging`, `build_service`, `default_data_dir`, `default_socket_path`, `_trigger_sentry_release_async`, `main`, binary-drift helpers) |
| Handler entries in dispatch dict | **293** |
| Handler methods (`_handle_*` on `BackendService`) | **84** |
| Dispatch entries calling extracted services directly | **219** (75%) |
| Dispatch entries still calling local `self._handle_X` | **74** (25%) |

### Section breakdown

| Section | Lines | LOC | Description |
|---------|-------|-----|-------------|
| Imports + preamble | 1–164 | **164** | 130 service imports + stdlib + sys.path setup |
| `BackendService.__init__` | 165–637 | **473** | Instantiates all 50+ collaborator objects, wires error bus, runs startup checks |
| `BackendService` helpers | 638–911 | **274** | `_init_llm_rewriter`, `_cached_settings`, `_get_runtime_setting`, proxy properties for RecordingCoreService, static `_error`/`_coerce_bool`/`_coerce_bounded` |
| `handle_request` + dispatch dict | 912–1319 | **408** | Single 293-entry dict + signing/throttle/breadcrumb middleware + try/except call |
| Remaining `_handle_*` methods | 1320–2477 | **1 158** | 80 handler methods, mix of thin delegation and real logic |
| `IPCServer` class | 2478–2592 | **115** | Unix socket accept loop + per-connection thread |
| Module-level utils | 2593–2746 | **154** | `configure_logging` + `JsonFormatter`, `build_service`, `main`, Sentry release helper |

The single biggest block (45% of file) is the 80 `_handle_*` methods that have not yet been extracted to dedicated services. The dispatch dict (15%) is a close second and is the main friction point when adding new handlers.

---

## 2. Natural Split Lines

The file has four clearly separable concerns that map to distinct files. The splits are ordered by coupling risk (lowest first).

### 2A. `backend/service_logging.py` — Logging infrastructure

**What moves:**
- `configure_logging(data_dir: Path) -> None`
- Inner `JsonFormatter` class (currently defined inside `configure_logging`)
- `_STANDARD_LOG_ATTRS` frozenset (currently defined inside the function)

**Why it's clean:** Zero dependencies on `BackendService` or `IPCServer`. Only imports `logging`, `json`, `sys`, `pathlib.Path`, and `core.config.settings`. Already referenced by `rest_server.py` which re-implements an equivalent formatter inline — a known drift point.

**Estimated LOC for new file:** ~65 (function + class + frozenset + imports)  
**LOC removed from service.py:** ~55 (the function body only; `configure_logging` call in `main` becomes `from backend.service_logging import configure_logging`)

**Risk:** **LOW.** Pure function. No circular imports. The only callers are `main()` in `service.py` and `KrabEar/main.py`.

---

### 2B. `backend/ipc_server.py` — Unix socket transport layer

**What moves:**
- `IPCServer` class (lines 2478–2592, 115 LOC)
- `default_data_dir() -> Path`
- `default_socket_path(data_dir: Path) -> Path`
- The four `ipc_constants` imports at the top of `service.py` are already in `backend/ipc_constants.py` — no change needed there.

**Why it's clean:** `IPCServer.__init__` takes `socket_path: Path` and `service: BackendService` — it has a clean dependency boundary. It contains zero business logic; it only receives bytes, calls `service.handle_request(payload)`, and writes bytes back. The `default_data_dir` / `default_socket_path` utilities logically belong with the transport layer and are currently only used in `main()`.

**Estimated LOC for new file:** ~145 (class + 2 helpers + imports)  
**LOC removed from service.py:** ~130

**Risk:** **LOW.** `IPCServer` references `BackendService` only through the `service.handle_request()` call — a clean Protocol boundary. The only circular-import hazard is if `BackendService` ever imports `IPCServer`, which it does not. `KrabEar/main.py` imports both `build_service` and `IPCServer` from `service.py`; that import line changes to `from backend.ipc_server import IPCServer`.

---

### 2C. `backend/ipc_dispatch.py` — Handler dispatch table

**What moves:**
- The `handlers` dict literal (lines 920–1251, ~330 lines)
- The signing + throttle + breadcrumb middleware block (lines 1253–1319, ~67 lines)
- The `_BATCH_MAX_REQUESTS` constant and `_handle_batch` method (lines 1321–1376, ~56 lines)

**Proposed shape:**

```python
# backend/ipc_dispatch.py
from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable
if TYPE_CHECKING:
    from backend.service import BackendService

_BATCH_MAX_REQUESTS = 50

def build_handler_table(svc: "BackendService") -> dict[str, Callable]:
    """Returns the 293-entry method-to-handler mapping.

    Separated from BackendService to allow independent testing of the
    dispatch table without instantiating the full service.
    """
    return {
        "ping": svc._handle_ping,
        "start_recording": svc._handle_start_recording,
        # ... all 293 entries ...
    }

def handle_batch(svc: "BackendService", params: dict) -> dict:
    ...
```

`BackendService.handle_request` then becomes:

```python
def handle_request(self, payload):
    from backend.ipc_dispatch import build_handler_table
    method = ...
    handlers = build_handler_table(self)
    handler = handlers.get(method)
    # signing + throttle + breadcrumb middleware (stays here or also moves)
    ...
```

**Estimated LOC for new file:** ~430  
**LOC removed from service.py:** ~395 (dispatch dict + middleware + batch handler)

**Risk:** **MEDIUM.** The `build_handler_table` function receives `self` (the `BackendService` instance) and binds all handler methods. This creates a soft forward-reference — `ipc_dispatch.py` must not import `BackendService` at module level (use `TYPE_CHECKING` guard). The main hazard is that `handle_request` currently builds the dict on *every call* (O(n) allocation, 293 entries). Factoring it into a function called per-request preserves this behaviour. A `@cached_property` approach would be a safe optimisation but is out of scope for this spec.

**Test impact:** Tests that call `service.handle_request(...)` directly are unaffected — the public interface does not change. Tests that introspect `handlers` as a local variable will break (none currently do this per audit).

---

### 2D. `backend/backend_service_core.py` — Remaining BackendService logic

This is the residual: after 2A+2B+2C are extracted, `service.py` still contains:

- 164 LOC of imports
- 473 LOC of `__init__`
- 274 LOC of helpers/proxies
- 1 158 LOC of `_handle_*` methods (trimmed to ~830 after dispatch dict moves)
- 30 LOC of `build_service` factory + `main()`

**Total residual: ~1 870 LOC** — still large, but no longer mixed with transport or configuration concerns.

At this stage the recommended next wave is continued handler extraction (following the Wave 638 pattern) to move the 74 remaining `self._handle_X` methods. The top candidates from Wave 638 analysis:

| Cluster | Methods | Est. LOC | Target file |
|---------|---------|---------|-------------|
| TranscriptionJobService | 5 | ~450 | `transcription_job_service.py` (new) |
| ErrorBusHandlers | 8 | ~220 | `error_bus_service.py` (new) or expand `error_reporter.py` |
| DiagnosticsHandlers | 6 | ~150 | Consolidate into `health_check_service.py` (existing) |
| AudioDeviceHandlers | 3 | ~90 | Fold into `audio_analytics_service.py` (existing) |

These are Phase 2 and not part of this spec.

---

## 3. Proposed File Inventory After Split

| File | LOC (est.) | Classes | Role |
|------|-----------|---------|------|
| `backend/service.py` | **~1 870** | `BackendService` | Business logic hub + `__init__` + handler methods |
| `backend/ipc_server.py` | **~145** | `IPCServer` | Unix socket accept/dispatch |
| `backend/ipc_dispatch.py` | **~430** | — | Handler lookup table + middleware |
| `backend/service_logging.py` | **~65** | `JsonFormatter` | Logging config + JSON formatter |
| **Total (was 2 746)** | **~2 510** | | −236 LOC net (redistribution, some import duplication) |

Net reduction in `service.py` itself: **−876 LOC (32%)**.

---

## 4. Risk Assessment Per Split

| Split | Risk | Reason | Mitigation |
|-------|------|--------|------------|
| 2A — `service_logging.py` | **LOW** | Pure function, no class dependencies | Single PR, trivial |
| 2B — `ipc_server.py` | **LOW** | Clean Protocol boundary, zero business logic | Update `main.py` import; grep for `IPCServer` callers first |
| 2C — `ipc_dispatch.py` | **MEDIUM** | Forward reference to `BackendService`; per-call dict rebuild; 293 entries must be bit-exact | Run full dispatch-invariant test suite after (`make dispatch-tests`); verify no handler regressions |
| 2D — Residual handler extraction | **MEDIUM–HIGH** | Each handler may reference `self._*` attributes; requires dependency injection or `self` pass-through | Follow established Wave 638 extraction pattern; one service per PR |

**Circular-import risk matrix:**

```
service.py          → imports → ipc_dispatch.py     (OK: deferred import inside handle_request)
ipc_dispatch.py     → TYPE_CHECKING → service.py    (OK: runtime-free)
ipc_server.py       → imports → service.py          (OK: only BackendService type used)
service_logging.py  → imports → core.config.settings (OK: already a dependency)
```

No circular imports are introduced by any of the four splits.

---

## 5. Recommended Implementation Order

### Step 1 — `service_logging.py` (lowest risk, independent PR)

1. Create `backend/service_logging.py` with `configure_logging` + `JsonFormatter`.
2. Replace `configure_logging` definition in `service.py` with `from backend.service_logging import configure_logging`.
3. Update `rest_server.py` to import from `service_logging` instead of defining its own inline JSON formatter.
4. CI: `make lint` + `make audit-orphans`.

**Estimated effort:** 1 hour. Single PR.

---

### Step 2 — `ipc_server.py` (clean transport extraction)

1. Create `backend/ipc_server.py` with `IPCServer`, `default_data_dir`, `default_socket_path`.
2. In `service.py`: remove the class and two functions; add `from backend.ipc_server import IPCServer, default_data_dir, default_socket_path` at top.
3. Update `KrabEar/main.py` if it imports `IPCServer` from `service`.
4. CI: `make dispatch-tests` + `make audit-orphans`.

**Estimated effort:** 2 hours. Single PR.

---

### Step 3 — `ipc_dispatch.py` (highest-value, medium risk)

1. Create `backend/ipc_dispatch.py` with `build_handler_table(svc)` returning the dict.
2. Move `_handle_batch` and `_BATCH_MAX_REQUESTS` into `ipc_dispatch.py` as module-level items.
3. Rewrite `BackendService.handle_request` to call `build_handler_table(self)` then proceed with existing signing/throttle/breadcrumb/try-except.
4. Run `make dispatch-tests` — target: 290+ handlers passing invariant checks.
5. Run `make service-loc` — target: `service.py` ≤ 1 900 LOC.

**Estimated effort:** 3–4 hours. Single PR with careful review.

**Do not cache the handler table** (e.g. `@cached_property`) in this step — that is a separate optimisation wave with different tradeoffs (thread-safety for hot-reload).

---

### Step 4 — Continued handler extraction (Phase 2, future waves)

Follow Wave 638 pattern for remaining 74 local `_handle_*` methods. Prioritise `TranscriptionJobService` first (~450 LOC, highest impact). Each extraction = independent PR.

---

## 6. Test Strategy

No new tests are required for the structural split (Steps 1–3) because:

- `BackendService.handle_request` public interface is unchanged.
- `IPCServer` public interface is unchanged.
- The dispatch invariant tests (`KrabEar/tests/test_dispatch_invariants_wave693.py`) will continue to verify all 293 handlers resolve without error.
- The orphan-import audit script (`scripts/audit_orphan_imports.py`) will catch any dropped imports.

**Pre-merge checklist for Step 3:**
- `make dispatch-tests` — all dispatch invariant tests pass
- `make audit-orphans` — zero orphan imports
- `make service-loc` — service.py at or below 1 900 LOC
- `make lint` — flake8 zero warnings

---

## 7. Non-Goals

This spec does **not** propose:

- Moving `BackendService.__init__` logic into a builder pattern (DI container) — separate architectural decision.
- Caching the handler table (performance optimisation, separate wave).
- Breaking `BackendService` itself into multiple classes — the existing service extraction pattern (14 extracted services) already handles this at the domain level.
- Any changes to the Swift agent, IPC protocol, or JSON-RPC wire format.
- New tests beyond what is needed for regression coverage.

---

## 8. Summary

A 4-way split reduces `service.py` from **2 746 to ~1 870 LOC (−32%)** by extracting three orthogonal concerns into dedicated files:

| Priority | File | LOC | Risk |
|----------|------|-----|------|
| 1 | `service_logging.py` | ~65 | LOW |
| 2 | `ipc_server.py` | ~145 | LOW |
| 3 | `ipc_dispatch.py` | ~430 | MEDIUM |
| 4 | Continued handler extraction | variable | MEDIUM-HIGH |

The split is fully backward-compatible at the API level. The recommended order is: logging → transport → dispatch → continued handler extraction. Step 3 (dispatch table) carries the highest execution risk and should be treated as a dedicated PR with full dispatch-invariant test gating.
