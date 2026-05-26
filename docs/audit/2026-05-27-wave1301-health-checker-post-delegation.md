# W1301 Re-audit: `health_check_service.py` + `health_checker.py` — Post-Delegation Residual Findings

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` (post Wave 423 extraction, post W1181 re-audit)
**Files audited:** `KrabEar/backend/health_check_service.py`, `KrabEar/backend/health_checker.py`
**Prior audits:** W953 (5 findings F1–F5), W1181 (5 findings, W963/W1187 not merged)

---

## Executive Summary

W963 (PR #882, `warming_up` status) and W1187 (PR #1099, wire delegation) are **both still OPEN** — neither has merged into `codex/krab-ear-v2`. The W1181 findings (F1: cold-start false-positive, F3: HealthCheckService orphan) therefore persist in production unchanged.

Beyond the unmerged-PR carry-overs, this audit identifies **5 new residual issues** specific to the post-delegation design, focusing on: delegation correctness after both PRs merge, edge cases not covered by W963, dead constructor parameters, and test isolation gaps.

---

## Merge State

| PR | Wave | Status | Subject |
|----|------|--------|---------|
| #882 | W963 | **OPEN** | `_check_stt_model` returns `warming_up` on cold-start; adds `warming_up` to `_aggregate_status` degraded set |
| #1099 | W1187 | **OPEN** | Wire `HealthCheckService` into `BackendService.__init__`; replace 6 inline duplicates with single-line delegations |
| #1087 | W1181 | **OPEN** (audit doc) | Previous re-audit; W963 not merged; F3 = orphan; F4 = APFS disk false-positive |

`HealthCheckService` is instantiated **nowhere** in the current `service.py` (`grep` confirms 0 occurrences of `HealthCheckService` or `_health_check_svc` in `KrabEar/backend/service.py`).

---

## Findings

### F1 — `get_diagnostics` bypasses `warming_up` status entirely (MED)

**File:** `KrabEar/backend/health_check_service.py:107–161`

`handle_get_diagnostics` reads STT state directly from `transcriber.engine` attributes:

```python
"stt": {
    "model_balanced": _global_settings.MODEL_BALANCED,
    "current_model": getattr(self._transcriber.engine, "current_model", None) if self._transcriber else None,
    ...
}
```

There is **no `status` key** in this dict. After W963 merges, `handle_health_check` will correctly surface `warming_up`/`degraded` during cold-start — but `handle_get_diagnostics` returns `current_model: null` with no context. A caller of the `get_diagnostics` IPC method cannot distinguish:
- model warming up (transient, expected)
- model absent (configuration error, persistent)

The `health_check` IPC method is correct for monitoring. But debug panels (history panel Diagnostics tab) call `get_diagnostics` and will silently show a null STT model with no status indicator.

**Fix:** Add `"stt_status": self._health_checker._check_stt_model().get("status", "unknown")` to the `stt` dict in `handle_get_diagnostics`. This delegates to the same `HealthChecker._check_stt_model()` that W963 fixes and avoids duplicate logic.

---

### F2 — `_check_stt_model` warming_up blind spot when `current_model` is set pre-load (MED)

**File:** `KrabEar/backend/health_checker.py:73–101` (W963 branch)

W963's fix adds the `warming_up` status in the `else` branch of `if current_model:`:

```python
if current_model:
    return {"status": "ok", "model": current_model, "cached": cached}
else:
    if not cached:
        return {"status": "warming_up", "model": None, "cached": False}
```

`AudioEngine.__init__` assigns `self.current_model` from config at construction time, **before** `_whisper_model` is loaded. Therefore during the pre-warmup window: `current_model = "mlx-community/whisper-small-mlx"` (truthy) but `_whisper_model = None`. The check enters the `if current_model:` branch and returns `{"status": "ok", "model": current_model, "cached": False}` — the false-positive is only suppressed when the model name comes from config fallback (current_model=None path).

The `warming_up` path in W963 is therefore **unreachable** in the normal production startup sequence. The W953 F1 false-positive persists in a different form.

**Fix:** Change the condition: return `warming_up` when `current_model is not None AND cached is False` (model name is known but not loaded into memory), not only when `current_model is None`.

---

### F3 — Two dead constructor parameters in `HealthCheckService` (LOW)

**File:** `KrabEar/backend/health_check_service.py:43–68`

`HealthCheckService.__init__` accepts `llm_probe` and `metrics_collector`:

```python
def __init__(self, ..., llm_probe: "LLMHttpProbe | None" = None,
             metrics_collector: "MetricsCollector | None" = None, ...) -> None:
    ...
    self._llm_probe = llm_probe          # stored, never used
    self._metrics_collector = metrics_collector  # stored, never used
```

Neither `self._llm_probe` nor `self._metrics_collector` is referenced in any handler method. W1187 wires both via `getattr(self, '_llm_probe', None)` in the `BackendService.__init__`, propagating the dead surface into the wiring callsite.

There are no tests for these parameters because there is nothing to test. This is confusing dead API surface that implies future methods will use them.

**Fix:** Either (a) remove both parameters from the constructor and the W1187 wiring callsite, or (b) add a comment documenting which future handler will use each (e.g. `_llm_probe` reserved for a future `probe_llm_latency` method).

---

### F4 — No in-branch integration test verifying service.py delegates to `HealthCheckService` (LOW)

**File:** `KrabEar/tests/` (missing file)

`test_health_check_service_delegation_W1187.py` exists on the `fix/wire-health-check-service-W1187` branch but does **not** exist in `codex/krab-ear-v2`. It will not land until W1187 merges.

Until then, the test suite covers:
- `test_health_check_service.py` — tests `HealthCheckService` in isolation with fake collaborators (38 tests)
- `test_health_checker.py` — tests `HealthChecker` in isolation (38 tests)

Neither verifies that `BackendService._handle_ping` actually calls `HealthCheckService.handle_ping`. If W1187 were reverted or a future refactor broke the delegation silently, no test would catch it.

This is acceptable while W1187 is pending, but becomes a gap risk if W1187 merges and the delegation test is accidentally omitted.

**Fix:** Merge W1187 as-is (it includes the delegation test). No additional action needed post-merge.

---

### F5 — `HealthMonitor.swift` `ping` contract: Swift only checks call success, not `status` field (INFO)

**File:** `native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift:105–186`, `main+HealthMonitor.swift:61`

HealthMonitor.swift only checks whether the `ping` call returns non-nil (call succeeds). It does **not** parse `result["status"]`:

```swift
return ((try? await client.callAsync(method: "ping", timeoutSec: 2)) != nil)
```

This means: if `handle_ping` returns `{"status": "error", ...}` it would still be counted as a healthy ping. The `status: "ok"` contract note in `HealthCheckService` is therefore stricter than what Swift actually enforces.

This is not a bug — it is intentional resilience (any response = backend alive). But the CLAUDE.md comment "контракт bit-exact — не менять поля / типы — HealthMonitor.swift парсит ответ" is **misleading**: Swift only checks call success, not field content. Future changes that add error conditions to ping could be misled by this comment.

**Fix:** Update the comment in `health_check_service.py:77–82` and `HealthCheckService.__doc__` to accurately describe that HealthMonitor checks call success (non-nil response), not the `status` field value.

---

## Non-Issues Confirmed

- **Delegation correctness (W1187):** After W963 and W1187 merge, `handle_health_check` delegates to `HealthChecker.check_all()` which includes the W963 `warming_up` fix. The delegation chain is correct for the `health_check` IPC method.
- **Merge order safety:** W963 modifies `health_checker.py`; W1187 delegates through it. Both orders work: W963-then-W1187 and W1187-then-W963 produce identical runtime behavior.
- **`_last_stt_engine_ref` shared reference:** W1187 passes the same mutable list `self._last_stt_engine_ref` to `HealthCheckService`. Updates by `BackendService` at `stop_recording` time are immediately visible through the shared reference. Correct.
- **`APP_VERSION` at construction time:** W1187 passes `APP_VERSION` (module constant) at `HealthCheckService.__init__` time. Since `APP_VERSION` never changes at runtime, `handle_ping` always returns the correct version. No dynamic version drift risk.
- **HealthMonitor restart cycle:** HealthMonitor calls `ping`, not `health_check`. The ping handler is a simple dict construction with no health_checker dependency. A stuck `HealthChecker.check_all()` (e.g. due to `sounddevice` blocking in `_check_audio_devices`) cannot block the HealthMonitor 3-second tick. Error isolation is solid.

---

## Merge Recommendation

1. Merge PR #882 (W963) first — fixes `_aggregate_status` to include `warming_up` in degraded set. Also addresses F2 partially (adds `warming_up` but has the blind spot described in F2 above).
2. Merge PR #1099 (W1187) second — wires delegation, eliminates 6 inline duplicates, adds delegation test.
3. Track F1 (`get_diagnostics` warming_up gap) as a post-merge follow-up.
4. Track F2 (W963 blind spot when `current_model` is set pre-load) as a separate fix wave.
5. F3 (dead params) is low-severity; can be cleaned up in a future tech-debt pass.
