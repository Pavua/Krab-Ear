# Wave 1169 — MetricsCollector residual audit

**Date:** 2026-05-26  
**Auditor:** W1169 (Claude Sonnet 4.6)  
**Branch:** `audit/metrics-collector-residual-W1169`  
**Base:** `codex/krab-ear-v2` @ `6c900317`  
**Files audited:**
- `KrabEar/backend/metrics_collector.py`
- `KrabEar/backend/service.py` (lines 2191–2226, `_handle_get_metrics_dashboard`)
- `KrabEar/backend/rest_server.py` (lines 359–464, metrics/prometheus endpoints)
- `KrabEar/backend/analytics_dashboard.py` (line 362–378, `_build_performance_info`)
- `KrabEar/tests/test_metrics_collector.py`
- `KrabEar/tests/test_metrics_collector_coverage.py`

---

## W966 / W971 merge-state verification

| Wave | Commit | Status on `codex/krab-ear-v2` |
|------|--------|-------------------------------|
| W966 | `1b04b314` — audit doc | **Present** (docs only) |
| W971 | `05d257bb` — NaN guard + nanpercentile fix | **NOT merged** |

W971 fix (`math.isfinite` guard in `record()`, `np.nanpercentile/nanmean/nanmin/nanmax` in `get_summary()`) was authored on a feature branch and exists in `audit/audio-quality-residual-W1100` but was never merged into `codex/krab-ear-v2`. The production codebase still uses bare `np.percentile` / `np.mean` / `np.min` / `np.max`.

---

## Findings

### F1 — W971 NaN guard NOT on `codex/krab-ear-v2` (HIGH)

**Location:** `KrabEar/backend/metrics_collector.py`, `record()` and `get_summary()`

**Current code (unfixed):**
```python
# record() — no finite check:
self.latencies.append(latency_ms)
self.confidences.append(confidence)

# get_summary() — bare percentile:
"p50": round(float(np.percentile(lats, 50)), 2),
"p95": round(float(np.percentile(lats, 95)), 2),
"p99": round(float(np.percentile(lats, 99)), 2),
"avg": round(float(np.mean(lats)), 2)
```

**Impact:** If `latency_ms` or `confidence` is `NaN` or `Inf` (from a misbehaving STT result or integer overflow in duration computation), `np.percentile` propagates the NaN into `get_summary()` output. Python's `json.dumps` serialises `NaN` as the bare token `NaN` which is **not valid JSON**. Swift's `JSONDecoder` and any strict HTTP client will reject the response, causing a silent analytics failure or UI crash.

**Reproduction:**
```python
mc = MetricsCollector()
mc.record(float('nan'), 0.9)   # enters deque
summary = mc.get_summary()     # p50/p95/p99/avg all NaN
import json; json.dumps(summary)  # produces '{"p50": NaN, ...}' — invalid JSON
# Swift JSONDecoder raises DecodingError
```

**Fix available:** W971 commit `05d257bb` — merge into `codex/krab-ear-v2`.

---

### F2 — `errors` and `total_requests` are unbounded lifetime counters, not sliding-window (MEDIUM)

**Location:** `KrabEar/backend/metrics_collector.py`, `__init__` (lines 25–26), `record()` (lines 32–34), `get_summary()` (lines 48–56)

```python
self.errors = 0          # increments forever
self.total_requests = 0  # increments forever
# ...
"error_rate": round(self.errors / self.total_requests, 4),
```

**Impact:** `latencies` and `confidences` are bounded deques (`maxlen=window_size`, default 1000). `errors` and `total_requests` are unbounded integers that grow for the lifetime of the process. The reported `error_rate` therefore reflects the entire process lifetime, not the last `window_size` requests. This contradicts both the class docstring ("скользящим окном") and the CLAUDE.md contract ("sliding-window metrics"). After 10 000 successful requests following an early error storm, the dashboard still shows a high error rate.

**Example:**
```python
mc = MetricsCollector(window_size=1000)
for _ in range(500):
    mc.record(0, 0, is_error=True)   # early errors
for _ in range(1000):
    mc.record(100.0, 0.9)            # recovery
mc.get_summary()["error_rate"]       # returns 0.333 — misleading
```

**Fix:** Track `_window_errors` as a sliding deque of booleans (same `maxlen=window_size`) and compute `error_rate` from `_window_errors` rather than the lifetime `errors / total_requests`. Keep `total_requests` for Prometheus `_total` counters (correct semantics there) but add a `window_error_rate` field.

---

### F3 — No minimum sample-count guard before reporting p95/p99 (LOW)

**Location:** `KrabEar/backend/metrics_collector.py`, `get_summary()` (lines 53–71)

With n < ~30 samples, `np.percentile(arr, 99)` is statistically meaningless (with n=2 it is just near the maximum of two values, with n=1 `p50 == p95 == p99 == avg`). The dashboard emits these values with no `sample_count` or `sample_count_warning` qualifier. Operators reading p99 = 2000 ms from a 2-sample window will misinterpret it as a high-sample-count tail estimate.

**Evidence:**
```python
# n=2: [100, 2000] ms
np.percentile([100, 2000], 99)   # → 1981.0 — looks like a real p99 but n=2
np.percentile([100, 2000], 50)   # → 1050.0
```

**Fix:** Add `"sample_count": len(lats)` to the `latency_ms` dict. Optionally add `"stats_reliable": len(lats) >= 30` so consumers can suppress low-confidence percentiles in UI.

---

### F4 — No public `reset()` method; test workaround mutates internals (LOW)

**Location:** `KrabEar/backend/metrics_collector.py` (no `reset()` method); `KrabEar/tests/test_metrics_collector_coverage.py` lines 207–211

The test `TestResetClearsAllMetrics` works around the missing API:
```python
with mc._lock:
    mc.latencies.clear()
    mc.confidences.clear()
    mc.errors = 0
    mc.total_requests = 0
```

This reaches into private state and is brittle — any future addition of a new counter would silently be missed. There is no IPC `reset_metrics` handler and no REST endpoint to reset metrics. The global `metrics` singleton accumulates data until the process is restarted.

**Fix:** Add a public `reset()` method on `MetricsCollector` that acquires `_lock` and clears all counters/deques atomically, then update the test to call it.

---

### F5 — IPC `get_metrics_dashboard` does not include `MetricsCollector` STT percentiles (LOW)

**Location:** `KrabEar/backend/service.py`, `_handle_get_metrics_dashboard` (lines 2191–2226)

The handler returns `session`, `preview_loop`, `llm`, `call_assist`, `import`, and `config_snapshot` but never calls `metrics.get_summary()`. CLAUDE.md states:

> **Metrics dashboard**: `get_metrics_dashboard` returns sliding-window latency percentiles, confidence stats, and diarization usage rate from `MetricsCollector`.

In practice, the STT latency percentiles are only available via the REST server (`GET /metrics` at port 5005), not the Unix-socket IPC used by the Swift agent. Any Swift panel that calls `get_metrics_dashboard` via IPC receives no `stt_metrics` field.

The REST path (`rest_server.py:364`) and `analytics_dashboard.py:366` correctly call `metrics.get_summary()` — only the IPC handler is missing it.

**Fix:** Add to `_handle_get_metrics_dashboard`:
```python
from backend.metrics_collector import metrics as _metrics
# ...
"stt_metrics": _metrics.get_summary().get("stt_metrics"),
```

---

## Privacy / IPC concurrency checklist

| Concern | Verdict |
|---------|---------|
| Transcript text in metric tags | Not present. Only numeric `latency_ms` and `confidence` float are stored. |
| Privacy-mode bypass | Not applicable — no text is ever captured. |
| `record()` / `get_summary()` race | `threading.Lock` correctly serialises both. `list(self.latencies)` snapshot inside lock prevents mid-eviction read. No race. |
| IPC handler thread vs metrics emitter | REST server calls `metrics.record()` after transcription; `get_summary()` may be called concurrently from dashboard requests. Both paths hold the same `_lock` — safe. |
| Memory growth bound | `deque(maxlen=window_size)` caps latency/confidence memory. **`errors` and `total_requests` are unbounded integers** — negligible memory (two `int` objects) but semantically wrong (see F2). |

---

## Test coverage post-W971 (on `codex/krab-ear-v2`)

34 tests pass (17 in `test_metrics_collector.py`, 17 in `test_metrics_collector_coverage.py`). **Zero tests** cover NaN/Inf input because W971 was not merged. The following scenarios lack coverage:

- `record(float('nan'), 0.9)` — currently crashes downstream via `np.percentile`
- `record(float('inf'), 0.9)` — same
- `get_summary()` output validated with `json.dumps` + strict parser

---

## Summary table

| ID | Severity | File | Description | Fix available |
|----|----------|------|-------------|---------------|
| F1 | HIGH | `metrics_collector.py` | W971 NaN guard not merged — `np.percentile` on NaN input → invalid JSON | Merge W971 commit `05d257bb` |
| F2 | MEDIUM | `metrics_collector.py` | `error_rate` uses lifetime counters, not sliding window | Sliding error deque |
| F3 | LOW | `metrics_collector.py` | No sample-count guard; p95/p99 with n<30 is misleading | Add `sample_count` field |
| F4 | LOW | `metrics_collector.py` | No `reset()` public method; tests mutate internals | Add `reset()` |
| F5 | LOW | `service.py` | IPC `get_metrics_dashboard` missing `stt_metrics` from MetricsCollector | Add `metrics.get_summary()` call |
