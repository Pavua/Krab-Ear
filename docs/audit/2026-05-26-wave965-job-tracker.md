# Wave 965 — JobTracker Security & Correctness Audit

**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/job_tracker.py` + `KrabEar/backend/recording_core_service.py` (caller)  
**Tests reviewed:** `KrabEar/tests/test_job_tracker.py`  
**Method:** static read-only analysis

---

## Summary

`JobTracker` is a well-structured, minimal in-memory state store. Thread safety is solid and the
prune mechanism is functional. Five findings were identified — one HIGH, two MEDIUM, two LOW.

---

## Findings

### HIGH — Stale "running" Jobs After Worker Crash (No Watchdog)

**File:** `job_tracker.py:41`, `recording_core_service.py:396-436`

`_worker()` runs in a daemon thread. If the Python process survives but the thread dies due to an
unhandled exception *outside* the top-level `try/except` (e.g., in a callback registered with a
C-extension, or `BaseException` such as `KeyboardInterrupt`), the job stays in `status="running"`
forever. Because `prune()` only removes terminal statuses (`done`, `failed`, `cancelled`), these
orphan jobs accumulate in `_jobs` and are never pruned — a slow memory leak with no upper bound.

**Evidence:** `prune()` at line 143 explicitly skips non-terminal statuses. The `_worker()` outer
try-except catches `Exception` (line 426) but not `BaseException`. Daemon thread death leaves no
`finished_at` set.

**Impact:** Memory growth proportional to crash frequency; stale job IDs returned to callers if
they retain references. No watchdog or max-running-age cleanup exists anywhere in the codebase.

**Recommendation:** Add a `max_running_age_sec` guard in `prune()`:
```python
# Also prune jobs stuck in "running" beyond a timeout (worker crash guard)
stuck_threshold = time.monotonic() - max_running_age_sec
stale += [
    jid for jid, job in self._jobs.items()
    if job.get("status") == "running"
    and (job.get("started_at") or 0.0) < stuck_threshold
]
```
A reasonable default is `max_running_age_sec=3600` (matches existing done-job TTL).

---

### MEDIUM — No State-Machine Guard on `update()` — Illegal Transitions Possible

**File:** `job_tracker.py:61-70`, `recording_core_service.py:398`

`update()` accepts arbitrary `**fields` including `status`, with no transition validation. Any
caller can write `status="running"` on a `done` job or `status="queued"` on a `failed` job. The
test suite even uses this pattern (`test_update_running_status`) to drive status directly via
`update()`, bypassing the intended lifecycle.

**Concrete risk in worker code:** `recording_core_service.py:398` calls
`self._job_tracker.update(job_id, status="running")` *before* calling `_transcribe_paths_core`.
If `mark_done` or `mark_failed` races and finishes first (extremely unlikely but theoretically
possible), the subsequent `update(status="running")` would reopen a terminal job.

**Recommendation:** Add an `ALLOWED_TRANSITIONS` guard inside `update()` when `status` is present
in `fields`, or factor status transitions into dedicated `mark_running()` / `mark_cancelled()`
methods (mirroring `mark_done` / `mark_failed`) that validate the current state before mutating.

---

### MEDIUM — Result Payload (`items`) Is Unbounded in Memory

**File:** `job_tracker.py:83-86`, `recording_core_service.py:362-372`

Full transcription results (including text, metadata, diarization segments) are accumulated in
`job["items"]` as a list of dicts. For batch imports of many large audio files, this list can grow
to tens of MB and stays in memory until `prune()` evicts the job (up to 1 hour after completion).

The IPC response handler in `handle_get_transcribe_progress` (`recording_core_service.py:449-450`)
withholds `items` until status is terminal, which is good for intermediate polling. However the
full list is retained in the `_jobs` dict at all times and is delivered as a single JSON blob on
the final poll — no streaming, no pagination, no size cap.

**Recommendation:** Either (a) store items in the `StateStore` / disk and keep only a count + file
reference in the job dict, or (b) cap `job["items"]` to a configurable
`MAX_JOB_RESULT_ITEMS` (e.g., 500) and truncate with a warning field, to prevent OOM on
pathologically large batch imports.

---

### LOW — `prune()` Called Outside the Lock in `create_job()`

**File:** `job_tracker.py:40, 57`

`create_job()` calls `self.prune()` (line 40) *before* acquiring `_lock`, then acquires `_lock`
separately to insert the new job (line 57). `prune()` itself acquires `_lock` internally. This is
correct for re-entrant safety (standard `threading.Lock` is not re-entrant) but produces a
momentary window where another thread could insert or delete jobs between the prune pass and the
new-job insertion. No data corruption results (both operations are lock-protected), but a freshly
created job *from another thread* that finishes and becomes terminal between the prune sweep and
the insert could be missed by prune for another full cycle.

This is a very minor ordering quirk, not a real bug. No action required unless the tracker is used
at very high creation rates.

---

### LOW — No Privacy-Mode Filtering on Job Result Items

**File:** `recording_core_service.py:439-473`, `job_tracker.py` (passive)

The `handle_get_transcribe_progress` response includes full `items` (with transcript text) when
status is terminal. No check is made against a `privacy_mode` setting at this layer. All other
transcript-returning IPC handlers in `service.py` gate on `privacy_mode` (e.g., `get_history`).
The async job path bypasses that gate entirely because it goes through `RecordingCoreService`
which has no reference to `SettingsService`.

**Impact:** If a user enables privacy mode *during* a long batch transcription job, completed
results from files transcribed before the mode change will still be exposed on the next
`get_transcribe_progress` poll. The exposure window is bounded by the job TTL (1 hour).

**Recommendation:** Inject or pass `privacy_mode` into `handle_get_transcribe_progress` and
conditionally redact `items[*].text` fields (replace with `"[privacy mode]"`) when the setting
is active, consistent with the pattern used in `handle_get_history`.

---

## Test Coverage Assessment

**Coverage: Good for happy paths; gaps on edge cases.**

| Area | Covered? |
|------|----------|
| ID generation (prefix, uniqueness, concurrent) | Yes |
| Initial state fields | Yes |
| `update()` arbitrary fields | Yes |
| `mark_done` / `mark_failed` | Yes |
| `cancel()` — flag set, terminal status guard, missing ID | Yes |
| `prune()` — old done/failed, preserve running, preserve recent | Yes |
| Thread-safety under concurrent updates | Yes (10 threads × 100 ops) |
| Concurrent `create_job` ID uniqueness (50 threads) | Yes |
| Unicode paths and error messages | Yes |
| **Illegal status transitions via `update()`** | Not tested |
| **cancel() vs mark_done() concurrent race** | Not tested |
| **Stale running jobs after simulated crash** | Not tested |
| **Result payload size bounds** | Not tested |
| **Privacy mode filtering of items** | Not tested |

---

## IPC Exposure Summary

Three IPC methods expose `JobTracker` state:

| Method | Sensitive data in response |
|--------|---------------------------|
| `transcribe_paths_async` | Returns only `job_id` — safe |
| `get_transcribe_progress` | Returns full `items` (transcript text, paths) when done — see privacy finding |
| `cancel_transcribe_job` | Returns only `{"cancelled": bool}` — safe |

No method exposes the full `_jobs` dict. Job IDs use 8-char hex UUID segments (`j-{hex8}`), which
provides 32 bits of entropy — sufficient for an in-process store but note that IDs are predictable
if an attacker can observe creation time and UUID generation is seeded from OS entropy (standard).

---

## Action Items

| Priority | Action |
|----------|--------|
| HIGH | Add `max_running_age_sec` guard in `prune()` to evict stuck "running" jobs |
| MEDIUM | Add state-machine transition validation in `update()` when `status` field is present |
| MEDIUM | Cap or offload `job["items"]` payload to prevent unbounded memory on large batches |
| LOW | Pass `privacy_mode` into `handle_get_transcribe_progress` and redact items text |
