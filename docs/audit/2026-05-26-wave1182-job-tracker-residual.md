# W1182 Re-audit: job_tracker residual after W972 (stale-running watchdog)

**Date:** 2026-05-26
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)
**Scope:** `KrabEar/backend/job_tracker.py`, `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/transcription_queue.py`, `KrabEar/tests/test_job_tracker.py`
**Prior waves:** W965 (initial audit, HIGH finding: stale-running memory leak), W972 (fix PR #893)

---

## W972 / W965 Merge State

| Branch | PR | Status |
|---|---|---|
| `docs/audit-job-tracker-W965` | #884 | **OPEN** (not merged) |
| `fix/job-tracker-watchdog-W972` | #893 | **OPEN** (not merged) |

Neither W965 nor W972 has been merged into `codex/krab-ear-v2`. The stale-running memory-leak fix is available on the feature branch but **not yet in production code**. All 5 findings below are assessed against the current `codex/krab-ear-v2` state (pre-W972), noting where W972 partially addresses an issue.

---

## W972 Fix Summary (from PR #893 diff)

W972 adds a `max_running_age_sec` parameter (default 7200 s) to `prune()`. Jobs with `status == "running"` and `age > max_running_age_sec` are deleted from `_jobs` with a WARNING log. `prune()` now returns the count of removed entries. Three new test cases in `PruneStaleRunningTestCase` cover eviction, young-job preservation, and combined count.

---

## Findings (5 NEW residual issues)

### F1 — HIGH: `prune()` is demand-driven, not time-driven; idle sessions never evict

**Severity:** HIGH
**File:** `KrabEar/backend/job_tracker.py:40`

`prune()` is called exclusively from `create_job()`. If a session has a stale-running job (worker crashed with `BaseException` before calling `mark_failed`) and no new async transcription jobs are subsequently requested, `prune()` is never invoked. The eviction timeout of 7200 s is therefore a lower bound only when new jobs keep arriving. In practice:

- A user imports a large batch of files (job gets stuck at, say, file 47 of 200 due to MLX SIGSEGV)
- No further imports are queued
- The stale `running` entry persists in `_jobs` for the entire session lifetime — potentially hours or days

**W972 status:** W972 adds the eviction logic inside `prune()` but does **not** add a periodic background sweep. The fix is incomplete for the no-new-job scenario.

**Recommendation:** Add a lightweight periodic prune call — either a `threading.Timer` (rearmable) or hook into the existing HealthMonitor 3-second ping path (`backend/service.py` → `_handle_ping`) to call `self._recording_core_svc._job_tracker.prune()` on each ping.

---

### F2 — MEDIUM: `prune()` evicts the registry entry but does NOT stop the zombie worker thread

**Severity:** MEDIUM
**File:** `KrabEar/backend/recording_core_service.py:392–395`, `job_tracker.py` (W972 branch)

When W972's `prune()` removes a stale-running job from `_jobs`, the worker thread (`daemon=True`) is **not notified**. The `_cancel_check()` closure reads:

```python
def _cancel_check() -> bool:
    state = self._job_tracker.get(job_id)   # returns None after eviction
    return bool(state and state.get("cancel_requested"))  # → False
```

After eviction `get(job_id)` returns `None`, so `_cancel_check()` returns `False` — meaning the worker sees no cancellation signal and continues processing files. On large batch imports this can tie up MLX GPU resources for hours after the entry has been evicted from memory.

**W972 status:** Not addressed. The PR only removes the dict entry.

**Recommendation:** Before deleting a stale-running job in `prune()`, set `job["cancel_requested"] = True` so the next `_cancel_check()` poll (which happens between files) sees the signal and stops the worker cleanly. Alternatively, `prune()` could return evicted job IDs so the caller can set a separate cancellation flag.

---

### F3 — HIGH: `TranscriptionQueue` (W1044) has no stale-`processing` watchdog and no prune at all

**Severity:** HIGH
**File:** `KrabEar/backend/transcription_queue.py`

`TranscriptionQueue` (wired via W1044 as IPC methods `enqueue_transcription`, `cancel_transcription`, `get_queue_status`, `list_transcription_queue`) has **zero eviction logic**:

1. **No `prune()` method.** All `completed`, `failed`, `cancelled`, and `processing` entries accumulate in `_jobs` forever. `list_queue()` / `handle_list_queue` IPC returns the unbounded set.

2. **`cancel()` only works for `STATUS_PENDING`.** A job that is already `STATUS_PROCESSING` cannot be cancelled via `cancel()` — it returns `False` immediately:
   ```python
   if job.status not in (STATUS_PENDING,):
       return False
   ```
   A stuck `processing` entry is therefore irremovable through the public API.

3. **`process_next()` marks a job `processing` but there is no external caller in the codebase** (`grep -rn "process_next" KrabEar/` returns no call sites outside tests). The queue is enqueued but never dequeued in production, meaning every `enqueue_transcription` IPC call adds a `pending` entry that will never transition — all entries eventually pile up as `pending` forever.

**W972/W965 status:** Neither wave touched `TranscriptionQueue`. This is a new gap exposed by W1044.

**Recommendation:** (a) Add a `prune(max_age_sec)` to `TranscriptionQueue` that evicts terminal entries. (b) Fix `cancel()` to also cancel `processing` entries. (c) Resolve the `process_next()` orphan — either wire a background worker or document the queue as scheduler-driven only.

---

### F4 — MEDIUM: `_worker()` catches `Exception`, not `BaseException`; leaves job stuck in `running`

**Severity:** MEDIUM
**File:** `KrabEar/backend/recording_core_service.py:426`

The async worker thread has:

```python
except Exception as exc:
    logger.exception("Async transcribe job %s упал", job_id)
    self._job_tracker.mark_failed(job_id, str(exc))
```

`BaseException` subclasses that are **not** `Exception` (`SystemExit`, `KeyboardInterrupt`, `GeneratorExit`) bypass this handler. If the Python process receives `SIGTERM` during a transcription and the interpreter raises `SystemExit` in the worker thread, `mark_failed()` is never called and the job stays in `running`. W972's watchdog will eventually clean this up after 7200 s, but during the interim the job appears permanently active to `get_transcribe_progress`.

**Note:** In Python, `KeyboardInterrupt` and `SystemExit` are not normally delivered to non-main daemon threads — the main thread handles them. However `GeneratorExit` from generator-based code inside `_transcribe_paths_core` is a realistic path if a generator is closed externally.

**W972 status:** Partially mitigated by eventual eviction at 7200 s, but not immediately.

**Recommendation:** Change `except Exception as exc:` to `except BaseException as exc:` followed by a `raise` for non-`Exception` types, or add a `finally` block in `_worker()` that checks if the job is still in `running` state and calls `mark_failed()` unconditionally:

```python
finally:
    state = self._job_tracker.get(job_id)
    if state and state.get("status") == "running":
        self._job_tracker.mark_failed(job_id, "worker exited unexpectedly")
```

---

### F5 — LOW: No `list_async_jobs` IPC; clients cannot discover active job IDs after reconnect

**Severity:** LOW
**File:** `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/service.py:928–930`

Only three IPC methods are wired for async job management:
- `transcribe_paths_async` — creates a job and returns `job_id`
- `get_transcribe_progress(job_id)` — polls a known job
- `cancel_transcribe_job(job_id)` — cancels a known job

There is no `list_async_jobs` IPC method. If the Swift agent restarts or reconnects (e.g., after an AGENT-K style crash/restart), it loses all in-flight `job_id` values from memory. There is no way to recover them or discover what jobs are still running. The user sees a spinner with no way to check progress or cancel.

**W972/W965 status:** Not addressed by either wave.

**Recommendation:** Add `list_async_jobs` IPC method returning `[{job_id, status, file_index, total_files, elapsed_sec}]` from `_job_tracker._jobs`. This is a read-only snapshot requiring only the existing `_lock`.

---

## Test Coverage Gap (post-W972)

W972 adds `PruneStaleRunningTestCase` (3 tests) covering eviction, young-job preservation, and combined count — adequate for the narrow fix. Gaps not covered by W972 tests:

| Scenario | Covered |
|---|---|
| Zombie thread continues after eviction (F2) | No |
| `prune()` never fires without `create_job()` (F1) | No (by design — unit test can't assert timing) |
| `TranscriptionQueue` stale-processing (F3) | No |
| `BaseException` path (F4) | No |
| `list_async_jobs` IPC (F5) | N/A (method missing) |

The existing `PruneTestCase.test_prune_preserves_running` in `codex/krab-ear-v2` incorrectly documents the pre-W972 behaviour as a feature ("даже если бы finished_at был искусственно старым — running не чистится") — this test will need updating when W972 merges to reflect the new eviction semantics.

---

## Summary Table

| ID | Severity | File | W972 fixes? | Issue |
|---|---|---|---|---|
| F1 | HIGH | `job_tracker.py:40` | Partially | `prune()` demand-driven; idle sessions never evict |
| F2 | MEDIUM | `recording_core_service.py:392` | No | Evicted job's worker thread continues; cancel flag not set |
| F3 | HIGH | `transcription_queue.py` | No | No prune at all; `processing` irremovable; `process_next()` not wired |
| F4 | MEDIUM | `recording_core_service.py:426` | Partially | `BaseException` bypasses `mark_failed()`; job stuck in `running` |
| F5 | LOW | `service.py:928–930` | No | No `list_async_jobs` IPC; job IDs unrecoverable after agent reconnect |

**Merge recommendation:** Merge W972 (PR #893) first as it fixes the original HIGH finding. Then address F1 (periodic prune) and F3 (TranscriptionQueue watchdog) as the next-priority items since both affect long-running production sessions.
