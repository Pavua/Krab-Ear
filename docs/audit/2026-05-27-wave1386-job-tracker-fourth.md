# Audit: job_tracker.py fourth-pass (W1386)

**Date:** 2026-05-27  
**Auditor:** Sub-agent W1386  
**Base branch:** `codex/krab-ear-v2` (HEAD `6c900317` — v2.0.5)  
**Files examined:** `KrabEar/backend/job_tracker.py`, `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/transcription_queue.py`, `KrabEar/backend/shutdown_handler.py`, `KrabEar/backend/service.py`, `KrabEar/tests/test_job_tracker.py`

---

## Merge state of prior fix waves

| Wave | Description | Commit | Merged into `codex/krab-ear-v2`? |
|------|-------------|--------|----------------------------------|
| W972 | stale-running watchdog in `prune()` | `e1d2ad88` | **NOT MERGED** |
| W1185 | `get_cancel_event()` API in `JobTracker` | (no separate commit; part of W1342 branch) | **NOT MERGED** |
| W1186 | periodic prune timer | (no commit found) | **NOT MERGED** |
| W1342 | wire `get_cancel_event` in `recording_core_service._cancel_check` | `e328bf53` | **NOT MERGED** |
| W1335 | third-pass audit doc | `caf57e2f` | **NOT MERGED** |
| W1044 | bulk_reprocess refuse during recording + mlx_lock | `f0b5443d` | **NOT MERGED** |

All four fix waves (W972 / W1185 / W1186 / W1342) remain on their own branches and have not been merged into `codex/krab-ear-v2`. The production `job_tracker.py` on `codex/krab-ear-v2` is identical to the original Wave 965 baseline: `prune()` evicts only terminal-status jobs, returns `None`, has no stale-running watchdog, no `get_cancel_event()`, no periodic background timer.

---

## Current state of `job_tracker.py` (codex/krab-ear-v2)

- `prune(max_age_sec=3600)` — evicts `{done, failed, cancelled}` jobs only; returns `None`; called from `create_job()`
- No `_cancel_events` dict, no `get_cancel_event()` method
- `cancel()` — sets `cancel_requested=True` flag; works on `queued` and `running` jobs (not on terminal)
- No background GC thread

---

## New findings (W1386)

### R1 HIGH — `prune()` called outside the lock in `create_job()` introduces a prune/insert TOCTOU gap

**Location:** `job_tracker.py:40` (`self.prune()`) and `job_tracker.py:57-58` (`with self._lock: self._jobs[job_id] = state`)

`create_job()` calls `self.prune()` before acquiring `self._lock` for the insertion. `prune()` itself acquires `_lock` internally. This means a second caller of `create_job()` on a different thread can interleave:

```
Thread A: self.prune()  ← acquires+releases _lock, evicts job X
Thread B: self.prune()  ← acquires+releases _lock (no-op, nothing left)
Thread A: with _lock: self._jobs[job_id_A] = state_A
Thread B: with _lock: self._jobs[job_id_B] = state_B
```

In this sequence both jobs are inserted correctly. The actual atomicity gap is different: a caller that reads `len(self._jobs)` between the prune call and the insertion will see the dict without the new job yet. More critically, if `prune()` is redefined to inspect queued/running jobs (as planned in W972), it could evict a brand-new job that was inserted by a concurrent thread that just finished its `with _lock: self._jobs[...] = ...` block before this thread called `prune()`. This is an existing latent bug that the W972 stale-running watchdog will trigger if it evicts jobs by `started_at` age without distinguishing brand-new queued jobs from actual stuck running jobs.

The correct fix is to run prune and insert atomically inside a single lock acquisition.

**Severity:** HIGH (latent; becomes active when W972 merged)

---

### R2 MEDIUM — `queued`-status jobs never evicted by `prune()` and never receive a `finished_at` timestamp

**Location:** `job_tracker.py:143–152` (prune terminal set), `job_tracker.py:45` (initial status), `recording_core_service.py:396–436` (_worker)

`create_job()` initializes `status="queued"`, `finished_at=None`. `prune()` only evicts `{done, failed, cancelled}`. If a worker thread exits with `BaseException` (e.g., `SystemExit` or `threading.ExceptHookArgs`) before it calls `self._job_tracker.update(job_id, status="running")` at line 398, the job remains in `queued` status with `finished_at=None` indefinitely. Even the pending W972 stale-running watchdog only checks `status == "running"`. A permanently-queued zombie job leaks memory until the backend process is restarted.

This is a distinct gap from W972's running-job watchdog: it covers the very narrow window between `create_job()` returning and `_worker()` executing its first line.

**Severity:** MEDIUM

---

### R3 MEDIUM — `shutdown_handler.shutdown()` does not cancel running `JobTracker` jobs

**Location:** `shutdown_handler.py:86–173` (`shutdown()` method), `service.py:724–735` (`close()`), `service.py:3857–3863` (signal handler)

When the backend receives `SIGTERM` or `SIGINT`, `_signal_handler` in `service.py` calls `server.stop()` then `service.close()`. `service.close()` only stops `_llm_probe`. The `GracefulShutdownHandler.shutdown()` is never called (it is instantiated but `.register()` is never invoked — confirmed: no call to `self._shutdown_handler.register(service)` in `service.py` or `main.py`). Even if it were called, `shutdown()` does not iterate over `_recording_core_svc._job_tracker._jobs` to cancel running jobs.

At SIGTERM the daemon worker thread continues to run transcription until it finishes or the process is killed. If `HealthMonitor` follows with `SIGKILL` after the timeout, the transcription is silently truncated and the job stays in `running` status in the eviction-dead zone.

**Severity:** MEDIUM

---

### R4 LOW — `TranscriptionQueue._jobs` is unbounded (no prune/evict ever called)

**Location:** `transcription_queue.py`, `service.py:414` (`self._transcription_queue = TranscriptionQueue()`)

`TranscriptionQueue` has no `prune()` method. Completed, failed, and cancelled queue jobs accumulate in `self._jobs` for the lifetime of the process. `list_queue()` returns all jobs including terminal ones. `get_queue_stats()` counts them all. This was first noted as W1335 R3 but phrased in terms of W1184 dequeue worker; the finding remains: on a long-running backend processing many file imports, `_jobs` grows without bound.

`JobTracker` mitigates this via `prune()` called from `create_job()`. `TranscriptionQueue` has no equivalent trigger and no caller ever prunes it.

**Severity:** LOW

---

### R5 LOW — `peek_transcription_queue` IPC handler is defined but not registered in service.py dispatch table

**Location:** `transcription_queue.py:358–361` (`handle_peek`), `service.py:1091–1094` (queue dispatch entries)

`TranscriptionQueue.handle_peek()` exists (IPC docstring: `peek_transcription_queue`). The service.py dispatch table registers `enqueue_transcription`, `cancel_transcription`, `get_queue_status`, and `list_transcription_queue`, but `peek_transcription_queue` is absent. Any Swift or test caller attempting `peek_transcription_queue` receives `{"ok": false, "error": "unknown method"}`.

**Severity:** LOW

---

## Summary

| ID | Severity | Description |
|----|----------|-------------|
| R1 | HIGH | `prune()` called outside lock in `create_job()` — TOCTOU gap; becomes critical after W972 merge |
| R2 | MEDIUM | `queued`-status jobs never evicted by prune; W972 watchdog misses them |
| R3 | MEDIUM | `GracefulShutdownHandler` not registered; no job cancellation on SIGTERM |
| R4 | LOW | `TranscriptionQueue._jobs` unbounded — no prune ever called |
| R5 | LOW | `peek_transcription_queue` IPC handler not wired into service.py dispatch table |

All prior fix waves (W972, W1185, W1186, W1342) remain unmerged into `codex/krab-ear-v2`. Merging them — especially W972 — without first addressing R1 will introduce an atomicity bug.
