# Audit: backend/metrics_collector.py — W1676

**Date:** 2026-05-30  
**Wave:** W1676  
**Auditor:** Sub-agent (read-only, first-pass)  
**File:** `KrabEar/backend/metrics_collector.py` (116 LOC)  
**Findings:** 6 (0 HIGH / 2 MED / 2 LOW / 2 INFO)

---

## Summary

`MetricsCollector` is a well-structured, small module. Thread-safety for the main write path (`record()`) and the primary read path (`get_summary()`) is correct: all mutations and the returned snapshot are guarded by a single `threading.Lock`. NaN/Inf guard is present and tested. Sliding-window memory bound is enforced by `deque(maxlen=window_size)`. The module is genuinely safe for the production scenario it operates in (low-frequency REST-only writes, occasional IPC reads).

Three residual issues are worth fixing:

- The `errors` compat property reads `error_events` **outside the lock** (F1).
- All numpy ops run **inside the lock** (F2) — not a crash risk today but a throughput hazard if polling frequency increases.
- `error_events` stores timestamps that are **never consumed** for time-based expiry (F6), making the stored values misleading.

Two architectural observations:

- The `get_metrics_dashboard` IPC handler **does not use `MetricsCollector`** at all (F3); latency percentiles are only reachable via the REST `/metrics` endpoint.
- `HealthCheckService` receives a `metrics_collector` constructor argument that is **stored but never read** (F5).

---

## Findings

### F1 — LOW: `errors` property reads `error_events` outside the lock

**Location:** `metrics_collector.py:34-36`

```python
@property
def errors(self) -> int:
    """Обратная совместимость: число ошибок в текущем окне."""
    return len(self.error_events)
```

`error_events` is mutated inside `_lock` by `record()` (line 55), but the `errors` property accesses it without acquiring the lock. Under CPython the GIL makes `len()` on a deque effectively atomic in practice, but the property is documented as a backwards-compat alias and callers cannot rely on that CPython implementation detail.

No production caller of `.errors` on the `MetricsCollector` singleton was found in a grep across `KrabEar/backend/` (excluding tests), so the impact is currently limited to test code that directly accesses the property. Fix: acquire `self._lock` inside the property, or document the GIL-only safety guarantee explicitly.

---

### F2 — MED: O(n log n) numpy operations execute while holding `_lock`

**Location:** `metrics_collector.py:78-111` — entire `get_summary()` body

`get_summary()` acquires `_lock` at line 78 and does not release it until line 111 (end of function). Inside this window it performs:

- Two `np.array(list(...))` copies (O(n))
- Four `np.nanpercentile` calls — each O(n log n) sort over up to 1000 samples
- Four `np.nanmean` / `np.nanmin` / `np.nanmax` calls

For `window_size=1000` (default) this is negligible today (~0.1 ms). The concern arises if a caller polls `get_summary()` frequently (e.g., a future dashboard auto-refresh at 1 Hz) while the REST path calls `record()` concurrently: every `record()` will block for the numpy sort duration.

**Fix pattern:** copy the deques while holding the lock, then release, then run numpy outside:

```python
def get_summary(self) -> Dict[str, Any]:
    with self._lock:
        lats = list(self.latencies)
        confs = list(self.confidences)
        total_requests = self.total_requests
        n_errors = len(self.error_events)
    # numpy ops outside the lock
    lats_arr = np.array(lats)
    ...
```

---

### F3 — MED: `_handle_get_metrics_dashboard` IPC handler does not consume `MetricsCollector`

**Location:** `KrabEar/backend/service.py:2400-2435`

The IPC method `get_metrics_dashboard` returns session recording state, LLM config, call assist state, and config snapshot — but **no latency percentiles or confidence stats**. `MetricsCollector` is not imported or called from `service.py` at all.

The only production caller of `metrics.record()` is `KrabEar/backend/rest_server.py:1112` and `1132` (REST `/transcribe` endpoint). This means:

- Clients using the IPC path (the Swift native agent) never see latency percentiles.
- The IPC `get_metrics_dashboard` response schema in `docs/IPC_API_REFERENCE.md` does not include STT latency data.
- `HealthCheckService` stores `metrics_collector` in `__init__` but never reads it (see F5).

If the intent is for `get_metrics_dashboard` to expose real-time STT metrics, `service.py` needs to import `from backend.metrics_collector import metrics` and merge `metrics.get_summary()` into the response.

---

### F4 — LOW: No public `reset()` method; test code bypasses lock to clear state

**Location:** `test_metrics_collector_coverage.py:208-212`

```python
with mc._lock:
    mc.latencies.clear()
    mc.confidences.clear()
    mc.error_events.clear()
    mc.total_requests = 0
```

Tests reset state by reaching into internals while holding the lock manually. This is reasonable for tests, but the absence of a public `reset()` means any future operator tooling (e.g., an IPC `reset_metrics` command) would need to replicate the same pattern. A minimal `reset()` method would codify the correct lock-protected clear semantics.

---

### F5 — INFO: `metrics_collector` injected into `HealthCheckService` but never used

**Location:** `KrabEar/backend/health_check_service.py:44,59`

```python
def __init__(self, ..., metrics_collector: "MetricsCollector | None" = None, ...):
    ...
    self._metrics_collector = metrics_collector
```

A search across all methods of `HealthCheckService` shows `self._metrics_collector` is stored but never referenced after `__init__`. The injection is dead weight. Either wire it into the diagnostics response (e.g., include `get_summary()` output in `get_diagnostics`) or remove the parameter.

---

### F6 — INFO: `error_events` stores monotonic timestamps that are never consumed

**Location:** `metrics_collector.py:55`

```python
self.error_events.append(time.monotonic())
```

`_error_rate()` only calls `len(self.error_events)` — it never inspects the stored timestamps. This gives the appearance of a time-windowed error rate ("sliding window") but actually implements a count-bounded window: errors age out only when newer errors push them out of the `maxlen` deque or when the whole deque is cleared.

A long-running backend that had a burst of 1000 errors 24 hours ago and no errors since will still report `error_events` containing 1000-entry timestamps from yesterday, inflating `error_rate`. A true time-based expiry would call `time.monotonic()` in `_error_rate()` and discard entries older than a configurable TTL (e.g., 5 minutes). If count-based expiry is intentional, the stored timestamps should be removed (store `1` instead of `time.monotonic()`) to avoid the misleading impression of time-based behaviour.

---

## Coverage Assessment

Existing test coverage is excellent for the scenarios tested:
- Empty window, single sample, full window, overflow: covered.
- NaN/Inf rejection: covered (`test_metrics_collector.py:295-339`).
- Thread-safety crash absence: covered (10-thread and 20-thread concurrent writes).
- Error rate sliding semantics: covered in `test_metrics_error_rate_sliding_W1191.py`.
- JSON serializability: covered.

Gaps:
- No test for the `errors` property race (F1): a test that calls `mc.errors` from one thread while `mc.record(is_error=True)` runs from another would expose the lock absence.
- No test for `reset()` via public API (there is no public API — F4).
- No test that verifies `get_metrics_dashboard` IPC includes STT latency data (because it currently does not — F3).

---

## Not a Finding

- **Window bound**: `deque(maxlen=window_size)` correctly enforces memory bound. No unbounded growth possible.
- **Time source**: `time.monotonic()` used — DST-safe.
- **Percentile algorithm**: `np.nanpercentile` is correct; edge cases (empty → early return, single sample → p50=p95=p99=value) handled.
- **JSON safety**: all numeric results wrapped in `float()` and `round()` before returning — no numpy scalar leakage.
- **Global singleton**: `metrics = MetricsCollector()` at module level is safe for the current single-process model.
