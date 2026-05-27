# W1335 Re-audit: `job_tracker.py` — Residual Findings After W972 / W1185

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` (HEAD `62df2ec9`)
**Files:** `KrabEar/backend/job_tracker.py`, `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/transcription_queue.py`, `KrabEar/tests/test_job_tracker.py`
**Prior waves:** W965 (initial audit), W972 (stale-running watchdog fix), W1182 (re-audit, 5 findings), W1185 (zombie cancel_event fix), W1184 (dequeue worker wire), W1044 (BulkReprocessor IPC wiring)

---

## Executive Summary

All four fix waves (W972, W1185, W1184, W1044) are on separate feature branches but
**none have been merged into `codex/krab-ear-v2`**. The current production file is the
153-line original `job_tracker.py` — no stale-running watchdog, no cancel_events, no
dequeue worker, no BulkReprocessor IPC handlers.

This audit re-examines the code in its current unpatched state and identifies 5 NEW
residual findings not previously documented in W1182. Findings F1–F5 from W1182 are
all still present; this document focuses only on gaps introduced or exposed since W1182
was written.

---

## W972 / W1185 / W1184 / W1044 Merge State

| Wave | Branch | PR | Merged into `codex/krab-ear-v2`? |
|------|--------|----|----------------------------------|
| W972 | `origin/fix/job-tracker-watchdog-W972` | #893 | **NO** |
| W1184 | `origin/wire-transcription-queue-dequeue-W1184` | N/A | **NO** |
| W1185 | `origin/fix/jobtracker-zombie-W1185` | N/A | **NO** |
| W1044 | `origin/wire-bulk-reprocess-W1044` | N/A | **NO** |

---

## New Residual Findings (5 NEW — distinct from W1182 F1–F5)

### R1 — MEDIUM: `test_prune_preserves_running` asserts old incorrect behaviour as a feature

**Location:** `KrabEar/tests/test_job_tracker.py:193`
**Severity:** MEDIUM (test contract drift)

The existing test explicitly documents pre-W972 semantics as correct:

```python
def test_prune_preserves_running(self) -> None:
    jid = self.tracker.create_job(1)
    self.tracker.update(jid, status="running")
    # Даже если бы finished_at был искусственно старым — running не чистится.
    self.tracker.update(jid, finished_at=time.monotonic() - 999999)
    self.tracker.prune(max_age_sec=1)
    self.assertIsNotNone(self.tracker.get(jid))
```

The comment "даже если бы finished_at был искусственно старым — running не чистится"
directly contradicts W972's design goal (evicting stale-running jobs after
`max_running_age_sec`). When W972 merges, this test will **fail** — not because W972
is wrong but because the test's assertion is wrong. It will block the W972 merge on CI
unless updated first.

The test should instead assert that: (a) a young running job is preserved, and
(b) a running job older than `max_running_age_sec` is evicted. Neither scenario is
covered today.

**Fix:** Split into two tests:
- `test_prune_preserves_young_running` — asserts `assertIsNotNone` for a running job
  that has been running for only a few seconds.
- `test_prune_evicts_stale_running` — artificially ages `started_at` past
  `max_running_age_sec`, calls `prune(max_running_age_sec=1)`, and asserts
  `assertIsNone`.

---

### R2 — MEDIUM: `_cancel_check` closure uses dict poll, not threading.Event; misses eviction signal

**Location:** `KrabEar/backend/recording_core_service.py:441`
**Severity:** MEDIUM (W1185 pre-condition not wired)

W1185 adds `get_cancel_event()` to `JobTracker` precisely so workers can obtain a
`threading.Event` reference and detect cancellation even after `prune()` evicts the main
dict entry. However the current `_cancel_check` closure in `recording_core_service.py`
only reads the dict:

```python
def _cancel_check() -> bool:
    state = self._job_tracker.get(job_id)   # returns None after eviction
    return bool(state and state.get("cancel_requested"))  # → False
```

After W972's eviction, `get(job_id)` returns `None`, so `_cancel_check()` returns
`False` — the zombie worker sees no cancellation and continues consuming MLX GPU.
W1185's fix (`get_cancel_event`) was designed to solve this, but the caller side
(this closure) has never been updated to use it.

This means W972 + W1185 together still leave the zombie worker problem unresolved
because the two halves of the fix have never been wired together.

**Fix:** After `create_job()`, fetch the cancel event:
```python
cancel_event = self._job_tracker.get_cancel_event(job_id)

def _cancel_check() -> bool:
    # Primary: threading.Event (survives prune() eviction — W1185)
    if cancel_event is not None and cancel_event.is_set():
        return True
    # Fallback: dict poll (for explicit cancel() calls before eviction)
    state = self._job_tracker.get(job_id)
    return bool(state and state.get("cancel_requested"))
```

---

### R3 — HIGH: `TranscriptionQueue` has no prune; `_jobs` dict is unbounded

**Location:** `KrabEar/backend/transcription_queue.py`
**Severity:** HIGH (memory leak, same pattern as original W965 finding)

`TranscriptionQueue` accumulates all jobs — `completed`, `failed`, `cancelled`,
`processing` — in `self._jobs` forever. There is no `prune()` method. Every
`enqueue_transcription` IPC call (once W1044 merges and wires `handle_enqueue`)
creates a persistent entry that is never removed.

The dequeue worker wired in W1184 processes jobs at a 2-second poll interval but
calls `mark_completed()` / `mark_failed()`, which transition the status but do not
remove the entry from `_jobs`. `list_queue()` / `handle_list_queue()` therefore
returns an ever-growing list to the Swift client.

For a background-running application that enqueues hundreds of audio files over
days or weeks, this is a slow but guaranteed memory leak. There is no production
trigger to flush it.

W1182 F3 noted the `process_next()` orphan (now fixed by W1184) and mentioned the
lack of prune, but treated it as a secondary concern. Now that W1184 wires an active
dequeue worker, the prune gap upgrades to HIGH: the queue will actually accumulate
terminal entries in production rather than just sitting idle.

**Fix:** Add `prune(max_age_sec: int = 3600)` to `TranscriptionQueue` that evicts
entries in terminal statuses (`completed`, `failed`, `cancelled`) older than
`max_age_sec` by `finished_at_iso` wall-clock time. Call it from `enqueue()` (same
pattern as `JobTracker.create_job()`).

---

### R4 — LOW: `TranscriptionQueue.cancel()` silently ignores `processing` jobs

**Location:** `KrabEar/backend/transcription_queue.py:cancel()`
**Severity:** LOW

```python
def cancel(self, job_id: str) -> bool:
    ...
    if job.status not in (STATUS_PENDING,):
        return False   # processing, completed, failed, cancelled → False
```

A caller who calls `cancel_transcription` IPC while a job is `processing` receives
`{"cancelled": False}` with no explanation. The job continues to run, and the status
entry persists because there is no mechanism to stop the dequeue worker mid-job (it
has no per-job cancellation signal). The user gets no feedback and cannot cancel a
running job.

This is not new — it was implicit in W1182 F3's "irremovable processing entry" note —
but with W1184's dequeue worker now wired, IPC callers will start hitting this
dead-end in production. The `False` return is indistinguishable from "job not found".

**Fix:** Return a structured error dict `{"cancelled": False, "reason": "already_processing"}` from `handle_cancel()` so the Swift client can display a meaningful message. Separately, add a per-job cancellation event to `TranscriptionQueue` so the dequeue worker can abort mid-transcription (mirrors the `cancel_event` pattern from W1185).

---

### R5 — LOW: `test_job_tracker.py` has no tests for the `get_cancel_event()` API added by W1185

**Location:** `KrabEar/tests/test_job_tracker.py`
**Severity:** LOW (coverage gap)

W1185 adds three new public methods / dict fields to `JobTracker`:
- `get_cancel_event(job_id) -> threading.Event | None`
- `_cancel_events` dict (set in `create_job`, set on `cancel()`, set before eviction in `prune()`)
- `_evict_times` dict (grace-period tracking)

`test_job_tracker.py` covers none of these. The `CancelTestCase` tests only assert
on `cancel_requested` in the state dict; they do not check that the returned
`threading.Event` is set after `cancel()`, that it is still accessible via
`get_cancel_event()` within the grace period after prune eviction, or that it becomes
`None` after the 2 s TTL expires.

The absence of tests for this API means regressions in the zombie-worker fix (W1185's
core correctness claim) would not be detected by CI.

`test_jobtracker_zombie_W1185.py` (added in the W1185 branch) covers these scenarios,
but that file is on `origin/fix/jobtracker-zombie-W1185` and has never been merged.

**Fix:** Merge W1185 (which brings `test_jobtracker_zombie_W1185.py`), or cherry-pick
its 5 test cases into `test_job_tracker.py` as a `CancelEventTestCase` class before
merging W1185 itself.

---

## Status of W1182 Findings in Current Code

| W1182 Finding | Severity | Fixed? |
|---------------|----------|--------|
| F1 — prune demand-driven; idle sessions never evict | HIGH | **NOT FIXED** — W972 not merged |
| F2 — zombie worker thread continues after eviction | MEDIUM | **NOT FIXED** — W1185 not merged |
| F3 — TranscriptionQueue no prune; process_next orphan | HIGH | **PARTIALLY** — W1184 wires dequeue worker (process_next orphan fixed), but no prune (R3 above) |
| F4 — `_worker()` catches `Exception` not `BaseException` | MEDIUM | **NOT FIXED** — recording_core_service.py:475 unchanged |
| F5 — No `list_async_jobs` IPC | LOW | **NOT FIXED** — not wired anywhere |

---

## Summary Table

| ID | Severity | File | Description |
|----|----------|------|-------------|
| R1 | MEDIUM | `test_job_tracker.py:193` | `test_prune_preserves_running` contracts old wrong behaviour; will block W972 merge |
| R2 | MEDIUM | `recording_core_service.py:441` | `_cancel_check` uses dict poll only; W1185 `get_cancel_event` never wired into caller |
| R3 | HIGH | `transcription_queue.py` | No `prune()`; `_jobs` grows unboundedly once W1184 dequeue worker is active |
| R4 | LOW | `transcription_queue.py:cancel()` | `cancel()` ignores `processing` jobs silently; indistinguishable from not-found |
| R5 | LOW | `test_job_tracker.py` | No tests for W1185 `get_cancel_event()` API; zombie fix unverifiable by CI |

**Merge recommendation:** Merge in order: W972 → W1185 → W1184 → W1044. Fix R1
(test contract) before merging W972 to avoid CI failure. Fix R3 (TranscriptionQueue
prune) before or alongside W1184. R2 requires updating `_cancel_check` after both
W972 and W1185 merge, since it wires the two halves together.
