# Wave 864 — IPC Throttle Audit

**Date:** 2026-05-26
**Scope:** `KrabEar/backend/ipc_throttle.py`
**Goal:** Verify token bucket math, `EXCLUDED_METHODS` list post-W801, and thread safety.
**Related:** W575 (coverage gaps), W801 (`call_check_auto_end` removal), W786 (dead-handler audit)

---

## 1. Token Bucket Math

### Implementation

`_TokenBucket.__init__` sets:
```python
self.capacity = float(capacity)
self.rate     = capacity / 60.0   # tokens/sec
self._tokens  = float(capacity)   # starts full
self._last_refill = time.monotonic()
```

`_refill()` uses a continuous-time leaky approach:
```python
elapsed = now - self._last_refill
self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
self._last_refill = now
```

### Verdict: CORRECT

- **Rate formula** `capacity / 60.0` is correct: a bucket with `capacity = 120` refills at 2 tokens/second, which gives exactly 120/min sustained throughput.
- **Burst semantics** are intentional: a fresh bucket starts full, so `capacity` calls are allowed immediately (burst), then the sustained rate applies. This matches the documented purpose (absorb momentary bursts, not prevent them).
- **Cap enforcement** `min(self.capacity, ...)` prevents token accumulation beyond capacity during idle periods.
- **`wait_time()` formula** `deficit / rate` is mathematically correct: `deficit = 1.0 - tokens`, `time_to_refill = deficit / (capacity/60) = deficit * 60 / capacity` seconds.
- **Floating-point precision**: `_tokens` starts as `float(capacity)` and arithmetic stays in float64 throughout. No integer truncation issues. The `>= 1.0` consume threshold is correct (not `> 1.0`).

### Limits by Category

| Category | Capacity | Rate (tokens/sec) | Max burst |
|----------|----------|-------------------|-----------|
| heavy    | 5        | 0.0833            | 5         |
| medium   | 30       | 0.5               | 30        |
| light    | 120      | 2.0               | 120       |

All three limits are reasonable for production IPC: a client that fires 5 consecutive LLM summarise calls will exhaust the heavy bucket in 5 calls, then wait ~12 s per additional call. The `light` burst of 120 covers the UI settings-slider scenario for non-excluded methods.

---

## 2. EXCLUDED_METHODS List — Post-W801

W801 (commit `3d62afd7`) removed `call_check_auto_end` from `EXCLUDED_METHODS` because the handler it was protecting was removed from the dispatch table entirely (confirmed dead, zero callers). That removal was correct.

### Current List (10 entries)

```python
EXCLUDED_METHODS = {
    "start_recording",
    "stop_recording",
    "get_recording_state",
    "set_paste_status",
    "ping",
    "set_settings",
    "get_settings",
    "apply_profile_preset",
    "list_profile_presets",
    "list_settings_backups",
    "restore_settings_backup",
    "create_manual_settings_backup",
    "translate_selection",
    "live_subs_ingest",
    "live_subs_stop",
    "call_estimate_cost",
}
```

(16 entries; counted in source.)

### Verification Against Live Dispatch Table

Cross-checked each entry against `service.py` handler registration:

| Method | In dispatch? | Exclusion rationale | Status |
|--------|-------------|---------------------|--------|
| `start_recording` | Yes | Recording lifecycle | OK |
| `stop_recording` | Yes | Recording lifecycle | OK |
| `get_recording_state` | Yes | Polling (real-time UI) | OK |
| `set_paste_status` | Yes | Paste lifecycle | OK |
| `ping` | Yes | Health-check, 3 s tick | OK |
| `set_settings` | Yes | Slider burst (20+ ev/s) | OK |
| `get_settings` | Yes | Read-only, no CPU cost | OK |
| `apply_profile_preset` | Yes | Settings write | OK |
| `list_profile_presets` | Yes | Static list | OK |
| `list_settings_backups` | Yes | Directory scan, rare | OK — borderline (see note) |
| `restore_settings_backup` | Yes | Write | OK |
| `create_manual_settings_backup` | Yes | Write | OK |
| `translate_selection` | Yes | Phase 2A per-selection | OK |
| `live_subs_ingest` | Yes | 10–30 chunks/sec audio | OK |
| `live_subs_stop` | Yes | Lifecycle | OK |
| `call_estimate_cost` | Yes | Polling from auto-end loop | OK |

**No stale entries** (all 16 resolve to live handlers in the dispatch table).

**`call_check_auto_end` is absent** — W801 removal verified correct.

**Minor note on `list_settings_backups`**: this is a directory scan, not a lifecycle call. It fits `light` (120/min) naturally if ever un-excluded; the exclusion is harmless but slightly over-permissive. Not a bug.

---

## 3. Thread Safety

### Lock Coverage

`IPCThrottle` uses a single `threading.Lock()` (`self._lock`) that wraps:

1. `check_rate()` — entire body after the `EXCLUDED_METHODS` early-return
2. `get_wait_time()` — entire body after the `EXCLUDED_METHODS` early-return
3. `get_throttle_stats()` — reads all shared state under lock
4. `reset_stats()` — writes all counters under lock

`_get_bucket()` is documented "call under lock" and is only invoked from within the lock in `check_rate` and `get_wait_time`.

### `_TokenBucket` is NOT thread-safe on its own

`_TokenBucket._refill()` and `consume()` have no internal lock. However, all callers hold `IPCThrottle._lock` before invoking them, so this is safe by design (single-threaded access per bucket).

### TOCTOU check

`get_wait_time()` calls `bucket.wait_time()` which internally calls `_refill()` again. This means between `check_rate()` returning `False` and the caller calling `get_wait_time()`, the bucket may partially refill. The returned wait time could therefore be slightly optimistic (shorter than actual wait needed). This is cosmetically incorrect — the error message might say "retry in 11.8s" when the actual refill takes 12.0 s — but it does not affect correctness of the rate limit itself.

### Verdict: CORRECT (with cosmetic TOCTOU note)

All mutable state (`_buckets`, `_call_counts`, `_throttled_counts`, `_total_calls`, `_total_throttled`) is accessed exclusively under `self._lock`. The test suite covers:

- `TestThreadSafety.test_concurrent_access_no_exception` — 10 threads × 50 iterations, no exceptions
- `TestThreadSafety.test_concurrent_throttle_count_consistent` — `allowed + throttled == total` invariant

Both pass.

---

## 4. Coverage Gaps (Carry-over from W575)

W575 identified 5 methods falling through to `light` despite being expensive. None have been promoted since that audit. Current state:

| Method | Current category | W575 recommendation | Gap action |
|--------|-----------------|---------------------|------------|
| `semantic_search` | light (120/min) | heavy (5/min) | Unimplemented |
| `semantic_search_reindex` | light (120/min) | heavy (5/min) | Unimplemented |
| `test_microphone` | light (120/min) | heavy (5/min) | Unimplemented |
| `get_keyword_cloud` | light (120/min) | medium (30/min) | Unimplemented |
| `get_sentiment_trends` | light (120/min) | medium (30/min) | Unimplemented |

These remain valid candidates. Impact is low in practice (no burst pattern observed in production logs), but the W575 proposals are still technically sound.

---

## 5. Summary of Findings

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| 1 | Token bucket math is correct (rate, cap, wait formula) | — | No action |
| 2 | `EXCLUDED_METHODS` has no stale entries post-W801 | — | No action |
| 3 | `call_check_auto_end` removal in W801 was correct | — | No action |
| 4 | Thread safety is correct; single lock covers all shared state | — | No action |
| 5 | Cosmetic TOCTOU: `get_wait_time` re-refills after `check_rate` returned False | info | Acceptable; document only |
| 6 | W575 coverage gaps (5 expensive methods at `light`) still unimplemented | low | Deferred; no new burst evidence |

**Overall:** Implementation is sound. No bugs found. W801 excluded-methods cleanup is verified complete.
