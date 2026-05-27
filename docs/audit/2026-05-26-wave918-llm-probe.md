# Wave 918 — LLMHttpProbe Audit

**Date:** 2026-05-26
**File:** `KrabEar/backend/llm_probe.py` (261 lines)
**Tests:** `KrabEar/tests/test_llm_probe.py` (267 lines, 8 test cases)
**Auditor:** Wave 918 automated audit

---

## Summary

`LLMHttpProbe` is a single-file background daemon thread that passively checks LM
Studio reachability every 30 s via GET `/api/v1/models`. It replaced an earlier
POST-based warmup probe (PR #364 F2) to eliminate JIT model-reload churn. Overall
the module is clean and well-tested. Four findings follow.

---

## Probe Interval

| Parameter | Default | Config key | Notes |
|-----------|---------|------------|-------|
| `base_interval_sec` | 30.0 s | `llm_probe_interval_sec` | Read once at `BackendService.__init__`; not refreshed at runtime |
| `max_interval_sec` | 300.0 s | — | Stored but never used; kept for API compatibility |
| `cold_load_threshold_ms` | 3000 ms | — | Stored but never used; same reason |
| `recovery_consecutive` | 3 | — | Stored but never used; same reason |

**FINDING 1 — MEDIUM: interval is read at startup, not refreshed at runtime.**

In `BackendService.__init__` (service.py:305):

```python
self._llm_probe = LLMHttpProbe(
    ...
    base_interval_sec=float(_settings_dict.get("llm_probe_interval_sec", 30.0)),
)
```

`_settings_dict` is fetched once. A subsequent `set_settings({"llm_probe_interval_sec": 60})` has
no effect on the running probe thread. The settings_provider lambda correctly fetches live
settings per tick for the enabled-check (`llm_rewrite_enabled`), but the interval is baked
into `self._current_interval_sec` at construction and never re-read.

**Impact:** Low in practice — 30 s default is sensible and users rarely tune this. But the
`llm_probe_interval_sec` key appears in the config dict, implying it is runtime-adjustable.

**Recommendation:** Read `settings.get("llm_probe_interval_sec", self._base_interval_sec)` at
the start of `_loop` when computing wait duration, or expose a `set_interval()` method.

---

## error_bus Integration

The probe pushes two `KrabError` codes:

| Code | Severity | Trigger | Dedup |
|------|----------|---------|-------|
| `rewriter.unavailable` | `info` | `passive_health_check` returns `(False, *)` | Via `ErrorBus` ring-buffer (external) |
| `rewriter.model_evicted` | `info` | `passive_health_check` returns `(True, False)` | Internal 600 s monotonic window |

Both push calls are wrapped in `try/except Exception` with a `logger.warning` fallback —
correct pattern per Phase B guidelines.

**FINDING 2 — LOW: `rewriter.unavailable` uses severity `info`, not `warn`.**

LM Studio being fully unreachable is a functional outage for LLM rewriting. The error
registry pattern (`error_codes.py`) classifies `rewriter.timeout` as `warn`. An unreachable
endpoint arguably warrants the same or higher severity so the ErrorToastPresenter shows it
for 5 s instead of 2 s (info).

**Recommendation:** Bump `rewriter.unavailable` KrabError severity from `"info"` to `"warn"`.

---

## Recovery Detection

The probe tracks liveness as `_alive: bool | None` (three-state):

| Transition | Action |
|-----------|--------|
| `None → True` | Silent (initial alive discovery) |
| `None → False` | Pushes `rewriter.unavailable` |
| `True → False` | Pushes `rewriter.unavailable` |
| `False → True` | Emits `rewriter_recovered` on EventBus |

The `rewriter_recovered` event is consumed by:
- `HealthMonitor.swift` → `subscribeToProbeEvents` → `flashGreen` on `StatusIndicatorView`

**FINDING 3 — LOW: `None → True` initial-alive transition is intentionally silent,
but there is no log at INFO level confirming initial probe success.**

When the backend starts with LM Studio already running, the first tick sets `_alive = True`
with no log output at INFO. The only observable signal is a debug log (not emitted by default).
Operators monitoring `journalctl` or `krab_tail_logs` have no confirmation that the probe
started healthy.

**Recommendation:** Add `logger.info("LLMHttpProbe: initial state → alive")` in `_on_state_change`
for the `old is None and new is True` case.

---

## Thread Lifecycle

`start()` / `stop()` mechanics:

```python
# start() — idempotent
self._thread = threading.Thread(target=self._loop, name="LLMHttpProbe", daemon=True)
self._thread.start()

# stop() — idempotent
self._stop_event.set()
self._thread.join(timeout=2)
self._thread = None
```

`_loop` uses `_stop_event.wait(interval)` — clean pattern; no `time.sleep` polling.

**FINDING 4 — LOW: `stop()` join timeout is 2 s with base interval 30 s.**

`stop()` calls `self._thread.join(timeout=2)`. The thread's inner `wait` is on `_stop_event`,
which is set before `join`, so the thread will wake immediately from its sleep. In practice
the join succeeds within ~1 ms after `_stop_event.set()`. However if `_tick()` is executing a
`passive_health_check()` when `stop()` is called, the join races against the HTTP timeout
configured in `llm_rewriter.passive_health_check()` (5 s timeout on the GET request).

If the HTTP call blocks for up to 5 s and `stop()` has a 2 s join timeout, the thread is
**not yet dead** when `stop()` returns and `self._thread = None`. The thread will finish
its current tick and then exit on the next `wait` check — safe because it's a daemon thread —
but `BackendService.close()` may log a false-negative.

**Recommendation:** Increase `stop()` join timeout to 8 s (5 s HTTP timeout + 2 s buffer +
1 s slack), or cancel the underlying session on stop by storing a `threading.Event` that
`passive_health_check` checks mid-flight.

---

## API-Compatibility Dead Parameters

Three constructor parameters (`cold_load_threshold_ms`, `max_interval_sec`,
`recovery_consecutive`) are accepted but never read after assignment. The docstring correctly
marks them "Kept for API compatibility" with a note about future cleanup. No action needed
now; a `#TODO(wave-next): remove` comment would make the cleanup intent machine-trackable.

---

## Test Coverage Assessment

| Test class | Scenario covered |
|------------|-----------------|
| `TestLLMHttpProbeAliveToDeadEmitsUnavailable` | alive → dead pushes `rewriter.unavailable` |
| `TestLLMHttpProbeDeadToAliveEmitsRecoveredEvent` | dead → alive emits `rewriter_recovered` |
| `TestLLMHttpProbeSkipsWhenDisabled` | `llm_rewrite_enabled=False` skips all checks |
| `TestLLMHttpProbeIntervalStaysFixed` (×2) | interval unchanged across ticks / on eviction |
| `TestLLMHttpProbeModelEvicted` (×3) | evicted pushes info code, dedup window, no push when loaded |

Missing coverage:
- `None → True` initial-alive path (no log assertion possible without spy)
- `stop()` called mid-tick (race with HTTP timeout)
- `settings_provider()` raising an exception (partially: skips tick — no assertion)

**Overall coverage: HIGH** for happy paths and primary state transitions. Missing edge cases
are low-risk given daemon-thread semantics.

---

## Findings Summary

| # | Severity | Area | Description |
|---|----------|------|-------------|
| 1 | MEDIUM | Probe interval | `llm_probe_interval_sec` baked at startup; runtime changes have no effect |
| 2 | LOW | error_bus | `rewriter.unavailable` severity is `info`; should be `warn` to match reachability outage weight |
| 3 | LOW | Recovery detection | No INFO log when probe discovers LM Studio healthy at startup (None → True) |
| 4 | LOW | Thread lifecycle | `stop()` join timeout (2 s) shorter than `passive_health_check` HTTP timeout (5 s); thread may outlive `close()` |
