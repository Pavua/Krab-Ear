# Wave 871 Audit: PerformanceProfiler (`backend/performance_profiler.py`)

**Date:** 2026-05-26
**Auditor:** Wave 871 sub-agent
**File audited:** `KrabEar/backend/performance_profiler.py` (123 LOC)
**Related files:** `KrabEar/core/engine.py`, `KrabEar/backend/llm_rewriter.py`, `KrabEar/backend/translator.py`, `KrabEar/backend/service.py`, `KrabEar/backend/health_check_service.py`, `KrabEar/tests/test_performance_profiler.py`, `KrabEar/tests/test_profiler_integration.py`

---

## Summary

6 findings across 4 categories: 0 critical, 2 medium, 3 low, 1 info.

| # | Severity | Category | Title |
|---|----------|----------|-------|
| F1 | MEDIUM | correctness | `total_profiled_time_sec` measures window work, not lifetime wall time |
| F2 | MEDIUM | correctness | `@profile` decorator silently misreports async functions (measures coroutine creation, not execution) |
| F3 | LOW | overhead | `get_profile_report` copies all deques to `list()` under lock — O(W×N) allocation on every diagnostics call |
| F4 | LOW | observability | `min_ms` absent from per-method stats — latency floor regressions undetectable |
| F5 | LOW | dead import | `performance_profiler` imported in `service.py` (line 116) but never used — dead import |
| F6 | INFO | design | Global singleton is module-level state; tests that use it without `reset()` will see cross-test data leak |

---

## F1 — MEDIUM: `total_profiled_time_sec` measures window work, not lifetime wall time

**File:** `KrabEar/backend/performance_profiler.py`, lines 88–89

```python
avg_ms = float(np.mean(arr))
total_time_ms += avg_ms * len(arr)
```

`arr` contains at most `window_size` samples (1000 by default). After the window rolls over, `len(arr) == window_size` regardless of how many calls were made. The formula therefore computes `avg_ms × window_size`, which approximates the cumulative time of only the last 1000 calls, not the lifetime total.

Example: 5000 calls each taking 1 ms, window=1000.
- `avg_ms` = 1.0, `len(arr)` = 1000
- `total_profiled_time_sec` = 1.0 s

But the real lifetime total = 5.0 s. The metric is 5× understated and will plateau as more calls arrive.

The field name `total_profiled_time_sec` implies a lifetime aggregate; the actual semantics are "sum of mean × count within the current sliding window." This misleads operators reading the diagnostics panel.

**Fix options:**
1. Rename to `window_profiled_time_sec` to document true semantics (no logic change).
2. Add a separate `lifetime_total_calls` counter (one `threading.Lock`-protected int per method name) updated in `_record()`.

---

## F2 — MEDIUM: `@profile` decorator silently misreports async functions

**File:** `KrabEar/backend/performance_profiler.py`, lines 55–63

```python
@functools.wraps(func)
def wrapper(*args, **kwargs):
    start = time.perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._record(name, elapsed_ms)
```

When `func` is an `async def`, calling `func(*args, **kwargs)` returns a coroutine object — it does not execute the body. The `finally` block fires immediately after coroutine creation, recording ~0.01 ms regardless of how long the coroutine actually runs when awaited by the caller.

No async functions in the current codebase are decorated with `@profiler.profile`, so this is not an active bug. However, the pattern is a latent trap: a developer adding `@profiler.profile` to an `asyncio` handler (e.g., a future IPC method) would see near-zero timings silently, with no error or warning.

**Fix:** Add an `asyncio.iscoroutinefunction` guard that raises `TypeError` at decoration time, or provide an async-aware branch:

```python
import asyncio
if asyncio.iscoroutinefunction(func):
    raise TypeError(
        f"@profiler.profile cannot be applied to async function {func!r}. "
        "Use 'async with profiler.start_span(name):' instead."
    )
```

---

## F3 — LOW: `get_profile_report` does O(W×N) allocation under lock

**File:** `KrabEar/backend/performance_profiler.py`, lines 78–79

```python
with self._lock:
    snapshot = {name: list(timings) for name, timings in self._data.items()}
```

For N tracked methods × window_size W samples each, this allocates N new Python lists of length up to W while holding the lock. At default W=1000 and N=20 methods (post-STT warm session), that is 20 000 float objects copied in the critical section.

This is acceptable in the current call frequency (diagnostics are pulled on-demand via IPC, not on a hot path). However, if a future change polls `get_profile_report` from a background health thread, lock contention with concurrent `_record()` calls in STT/translate/LLM threads could cause measurable latency spikes.

**Mitigation (no immediate action required):** If background polling is added, switch to a copy-on-write approach: maintain both a live deque and a pre-computed snapshot updated at configurable intervals.

---

## F4 — LOW: `min_ms` absent from per-method stats

**File:** `KrabEar/backend/performance_profiler.py`, lines 90–96

Current stats per method: `calls`, `avg_ms`, `p50_ms`, `p95_ms`, `max_ms`.

`min_ms` (the latency floor) is not reported. Without it:
- A regression where the fastest path slows down from 0.5 ms to 50 ms is invisible — `p50` and `p95` may not move if most calls are slow.
- The `max_ms - min_ms` spread (jitter) cannot be computed by consumers.

`np.min(arr)` is a one-liner addition alongside `np.max(arr)`.

**Fix:**

```python
"min_ms": round(float(np.min(arr)), 2),
```

Add to the `methods[name]` dict (no lock change needed).

---

## F5 — LOW: Dead import in `service.py`

**File:** `KrabEar/backend/service.py`, line 116

```python
from backend.performance_profiler import profiler as performance_profiler
```

A search of the entire `service.py` file finds no subsequent use of the `performance_profiler` name. The actual profiler report is fetched by `HealthCheckService` via its own local import (`backend/health_check_service.py`, line 110). The top-level import in `service.py` is dead weight.

**Impact:** Minor — adds one import to module load, zero runtime overhead. But it creates a misleading signal that `service.py` directly uses the profiler when it does not.

**Fix:** Remove line 116 from `service.py`.

---

## F6 — INFO: Global singleton — tests risk cross-contamination

**File:** `KrabEar/backend/performance_profiler.py`, line 123

```python
profiler = PerformanceProfiler()
```

The module-level singleton is shared across all importers in the same Python process. Test files that call production code which internally uses `start_span()` (e.g., `Translator.translate()`) will record spans into the global profiler as a side effect.

`test_profiler_integration.py` correctly calls `profiler.reset()` in `setUp()`. However, `test_performance_profiler.py` instantiates fresh `PerformanceProfiler()` objects for all tests and never touches the global singleton, so it is unaffected.

Risk is low given current test patterns, but any new integration test that forgets `reset()` in `setUp()` will inherit stale spans from the previous test, potentially causing flaky failures or false positives.

**Recommendation:** Document the `reset()` requirement in the module docstring:

```
NOTE: The module-level `profiler` singleton is shared process-wide.
Integration tests MUST call profiler.reset() in setUp() to prevent
cross-test data contamination.
```

---

## Positive findings

- **Thread safety is correct.** `_record()` uses a single `threading.Lock` protecting both the dict lookup and `deque.append`. `get_profile_report()` copies deques under the lock then runs numpy outside it — minimises lock hold time. No TOCTOU race.
- **No leaked profiler instances.** `SpanContext` holds a reference to the profiler but is created and discarded within each `with` block. The decorator closure holds a reference to `_record` but that is expected and correct.
- **Exception safety is correct.** `SpanContext.__exit__` always records elapsed time regardless of `exc_type`, and returns `None` (falsy) so exceptions propagate. The `@profile` decorator uses `finally` for the same guarantee.
- **Graceful import fallback.** All three callers (`engine.py`, `llm_rewriter.py`, `translator.py`) have a `try/except Exception` guard with a `_NoOpProfiler` fallback, ensuring STT/translate/LLM paths remain functional even if `numpy` is unavailable.
- **`deque(maxlen=window_size)` correctly enforces the sliding window** — Python's `deque` with `maxlen` auto-discards old entries, preventing unbounded memory growth.
- **Test coverage is comprehensive.** Both `test_performance_profiler.py` and `test_profiler_integration.py` cover the core contract, thread safety, nested spans, unicode names, zero-duration entries, and end-to-end IPC diagnostics integration.
