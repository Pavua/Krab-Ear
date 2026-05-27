# Wave 966 Audit: `MetricsCollector` — window, percentile, race, perf

**File:** `KrabEar/backend/metrics_collector.py` (77 lines)  
**Date:** 2026-05-26  
**Auditor:** W966 sub-agent (read-only)  
**Status:** 6 findings (3 medium, 3 low) — no critical, no PII leak

---

## Summary

`MetricsCollector` is a small, focused class: a thread-safe count-bounded sliding
window over latency and confidence samples, with `numpy` percentile calculation on
read.  The implementation is largely correct and privacy-clean.  Six issues were
found, none blocking for current usage patterns.

---

## Findings

### F1 — Count-based window only; no time-based expiry (Medium)

**Location:** `__init__`, `latencies = deque(maxlen=window_size)`

The window is purely count-based (`maxlen=1000`).  At high call rates (e.g. a
batch import of 5 000 audio files) all 1 000 slots fill quickly and the window
represents only the most recent ~few minutes of traffic.  Conversely, at low call
rates (1 recording/hour) 1 000 samples span weeks — P99 reflects ancient data.

Neither extreme is catastrophic (no crash, no OOM), but operators using the
dashboard to tune models see misleading latency percentiles.  A time-bounded ring
or TTL-tagged entries would fix this.

**Risk:** Misleading metrics; no memory or correctness risk.

---

### F2 — NaN/Inf in samples silently corrupt all percentiles (Medium)

**Location:** `get_summary()` → `np.percentile(lats, 50/95/99)`

`numpy.percentile` propagates `NaN`: if any single sample is `float('nan')` or
`float('inf')`, the returned values are `nan` or `inf`.  These then pass through
`round(float(...), 2)` — `round(float('nan'), 2)` returns `nan` in Python, which
is **not JSON-serialisable** and will crash `json.dumps` in the caller
(`analytics_dashboard.py`, `rest_server.py`).

`record()` performs no input validation; callers (e.g. `rest_server.py:936`) pass
`result.get("duration_ms", int(elapsed_sec * 1000))` which can theoretically be
`NaN` if the upstream engine returns one.

**Fix:** Either validate on `record()` (`math.isfinite` guard) or use
`np.nanpercentile` / `np.nanmean` in `get_summary()`.

**Risk:** JSON serialisation crash propagates to IPC callers and REST clients on
any NaN sample; low probability but non-zero.

---

### F3 — `reset()` method absent; tests simulate it by directly mutating internals (Low)

**Location:** `test_metrics_collector_coverage.py:TestResetClearsAllMetrics`

There is no public `reset()` or `clear()` method.  The coverage test works around
this by acquiring `_lock` and manually clearing the deques — an implementation
detail leak.  If `BackendService` or a future IPC handler needs to reset metrics
(e.g. after a model switch), callers have no clean API and must replicate the same
internal mutation.

No race condition is introduced by the current test (it holds the lock), but the
absence of a sanctioned API is a maintenance hazard.

**Risk:** Low — current callers do not call reset; hazard is future-only.

---

### F4 — `get_metrics_dashboard` IPC does not expose `MetricsCollector.get_summary()` (Low / Observation)

**Location:** `service.py:_handle_get_metrics_dashboard` (line 2192)

Despite the docstring in `CLAUDE.md` ("returns sliding-window latency percentiles,
confidence stats"), the actual handler returns session state, LLM status, call
assist state, and config snapshot — but **zero** latency/confidence data from
`MetricsCollector`.  The real metrics are only surfaced through `analytics_dashboard.py`
(`get_analytics_dashboard` IPC) via `_build_performance_info()`.

This is not a security issue, but callers querying `get_metrics_dashboard`
expecting P95 latency receive none.  The CLAUDE.md description is stale.

**Risk:** Documentation drift; no data leak.

---

### F5 — `np.percentile` is O(N log N) on each `get_summary()` call; no caching (Low)

**Location:** `get_summary()` — `np.array(list(...))` + `np.percentile` × 3

Every call to `get_summary()` sorts a copy of up to 1 000 latency samples.
`list(deque)` is O(N), `np.percentile` with default linear interpolation is
O(N log N).  For the default window of 1 000 this is ~10–50 µs on M4 Max —
acceptable in production.

However the method is called **inside the lock**, so a slow `get_summary()` (e.g.
window_size=10 000, called at 100 Hz) would block concurrent `record()` calls.
No actual problem at current settings; becomes a concern if window_size is
increased significantly.

**Risk:** Negligible at window_size=1 000; note for future if window is enlarged.

---

### F6 — `errors` counter unbounded; not windowed like latencies (Low)

**Location:** `record()`, `self.errors += 1`; `get_summary()`, `self.errors / self.total_requests`

`self.errors` and `self.total_requests` are lifetime counters, never capped.  In a
long-running session (days), `error_rate` approaches the true lifetime average, not
the recent window rate.  A burst of errors 6 hours ago is indistinguishable from a
current outage.

This contrasts with `latencies` / `confidences`, which are windowed.  The metric
is therefore internally inconsistent: `error_rate` is a lifetime ratio while
`p99_latency_ms` is a windowed metric.

**Risk:** Misleading dashboard; no memory risk (ints are unbounded in Python but
negligibly small).

---

## What is working well

- **Thread safety:** all mutable state (`latencies`, `confidences`, `errors`,
  `total_requests`) is accessed exclusively under `self._lock`.  No TOCTOU window
  between `record()` and `get_summary()` because both take the same lock.
  Snapshot copies (`np.array(list(...))`) are made inside the lock, so the lock is
  held for the full computation in `get_summary()` — correct if slightly
  conservative.

- **Memory bound:** `deque(maxlen=window_size)` provides a hard cap.  No
  unbounded growth on latency/confidence data.

- **JSON safety:** `round(float(np.percentile(...)), N)` converts numpy scalars to
  Python floats before return.  Confirmed by `TestExportMetricsSerializable`.
  (Exception: NaN/Inf as noted in F2.)

- **Privacy:** no transcript text, speaker labels, file paths, or user identifiers
  are recorded.  Only `latency_ms` (float) and `confidence` (float 0–1) are
  stored.  `get_metrics_dashboard` (F4) exposes only settings keys and boolean
  session flags — no PII.

- **Percentile accuracy:** `numpy.percentile` with default linear interpolation is
  exact (not approximate), at the cost of O(N log N) per call (F5).  No t-digest
  approximation is needed at N=1 000.

---

## Test Coverage

Two test files cover the module:

| File | Tests | Coverage |
|---|---|---|
| `test_metrics_collector.py` | 12 test methods | window eviction, percentiles, error rate, thread safety, empty state, boundary values |
| `test_metrics_collector_coverage.py` | 13 test methods (Wave 79) | same topics + JSON serialisability, simulated reset, large/negative latency, confidence boundaries |

**Gaps not covered by tests:**
- NaN/Inf sample propagation (F2) — no test inserts `float('nan')` or `math.inf`
- `errors` lifetime vs windowed inconsistency (F6)
- Behaviour under very high call rate (window_size=10 000, concurrent readers)

---

## Recommendations (priority order)

1. **[Medium / F2]** Add `math.isfinite` guard in `record()` or switch to
   `np.nanpercentile` + `np.nanmean` in `get_summary()`.  Add a test with
   `float('nan')` and `float('inf')` inputs.
2. **[Medium / F1]** Document the count-based window semantics in the class
   docstring.  If time-based expiry is needed, consider a lightweight TTL ring
   buffer or note it as a known limitation.
3. **[Low / F6]** Window `errors` alongside latencies (use a deque of booleans
   with the same `maxlen`), or document that `error_rate` is a lifetime figure.
4. **[Low / F3]** Add a public `reset()` method so callers have a clean API.
5. **[Low / F4]** Either add `MetricsCollector.get_summary()` output to
   `_handle_get_metrics_dashboard`, or update `CLAUDE.md` to point to
   `get_analytics_dashboard` as the correct latency/confidence endpoint.
