# Wave 924 Audit: TranscriptionQueue

**File:** `KrabEar/backend/transcription_queue.py`
**Date:** 2026-05-26
**Auditor:** W924 (read-only)

---

## Summary

`TranscriptionQueue` is a pull-model priority queue: no background worker thread is
created internally. The external caller (IPC dispatcher or a future worker) must call
`process_next()` to claim a job and then `mark_completed()` / `mark_failed()` when
done. The queue is in-memory by default; an optional `persist_path` enables NDJSON
persistence of `pending` jobs only.

**5 findings, 2 gaps, 0 critical bugs.**

---

## Finding 1 — Priority ordering is stable; FIFO on equal priorities is correct

`process_next()` and `peek()` both sort by `(priority, created_at)` where
`created_at` is `time.monotonic()` at job creation time — a float with nanosecond
resolution on macOS. Equal-priority jobs are therefore returned in strict insertion
order. The sort is done inside the lock over a freshly built list each call, so no
mutable shared state is involved. **No issue.**

---

## Finding 2 — Cancellation race: `cancel()` cannot cancel a running job (by design, but undocumented externally)

`cancel()` only accepts `pending` status:

```python
if job.status not in (STATUS_PENDING,):
    return False
```

Once `process_next()` flips a job to `processing`, cancellation returns `False`
immediately — the worker will write its result regardless. This is intentional
(comment says so in docstring) but the IPC handler `handle_cancel` just returns
`{"cancelled": false}` with no indication of *why* it failed. A caller that wants
to abandon a running transcription has no mechanism to do so; it must wait for
`mark_completed` / `mark_failed` or restart the backend.

**Risk:** low for normal operation, medium if a job transcribing a large file blocks
the queue loop indefinitely — no timeout mechanism exists in the queue layer.

**Recommendation:** document the `processing` limitation clearly in `handle_cancel`
response (add `"reason": "already_processing"`), and consider a `force_cancel`
flag that marks the job cancelled even from `processing` state so callers can at
least record the intent.

---

## Finding 3 — No background worker: `process_next()` is never called from within the module or from `service.py`

`service.py` instantiates `TranscriptionQueue()` and wires four IPC handlers
(`enqueue_transcription`, `cancel_transcription`, `get_queue_status`,
`list_transcription_queue`). **`process_next()` and `handle_peek` are not wired
as IPC methods and are not called from anywhere in production code** (confirmed by
`grep -rn "process_next" KrabEar/`).

This means the priority queue currently acts purely as a *bookkeeping* structure:
jobs can be enqueued and cancelled but will never actually be transcribed through
this mechanism. The actual batch transcription goes through the parallel
`transcribe_paths_async` / `JobTracker` path.

**Risk:** medium — the feature is effectively dead code in its current form. Callers
enqueuing via `enqueue_transcription` IPC will see jobs accumulate in `pending`
forever unless they manually poll `process_next()` over a separate mechanism.

**Recommendation:** either (a) wire `process_next()` to a background worker thread
started in `__init__` (or lazily on first `enqueue()`), or (b) expose
`process_next` as an IPC method so a Swift or external orchestrator can drive
processing manually. Document this design choice explicitly.

---

## Finding 4 — Crash isolation: worker crash does not affect the queue, but job leaks to `processing` forever

Because processing is external, a worker that crashes between calling
`process_next()` and `mark_completed()` / `mark_failed()` leaves the job stuck in
`status = processing`. There is no deadline, heartbeat, or watchdog inside
`TranscriptionQueue` to detect stale `processing` entries and reset them.

**Risk:** medium. After a backend restart, `_load()` only restores `pending` jobs
(by design), so the stale `processing` entry is simply lost. If `persist_path` is
not set (current production default — `TranscriptionQueue()` with no argument), all
in-flight jobs vanish on restart with no recovery path.

**Recommendation:** add a configurable `processing_timeout_sec` (e.g., 30 min).
A watchdog check inside `process_next()` or a separate `prune_stale()` method can
reset jobs older than the timeout from `processing` back to `pending`.

---

## Finding 5 — Memory growth: no bound on queue size; completed/failed/cancelled jobs accumulate indefinitely

`_jobs` dict grows without bound. A job is never removed from `_jobs` — it
transitions to terminal states (`completed`, `failed`, `cancelled`) and stays
forever. There is no equivalent of `JobTracker.prune()` in `TranscriptionQueue`.

With 1000 audio files enqueued: all 1000 `TranscriptionJob` objects (each ~300
bytes of Python object overhead plus the dict) live in memory until the process
restarts. For typical desktop use this is unlikely to be a problem, but it is a
design asymmetry: `JobTracker` has `prune(max_age_sec=3600)`, `TranscriptionQueue`
has nothing.

**Risk:** low in normal use, medium in unattended/server use where queues accumulate
over days without restart.

**Recommendation:** add a `prune(max_age_sec)` method mirroring `JobTracker` and
call it lazily from `enqueue()`.

---

## Finding 6 (gap) — Persistence not enabled in production

`service.py` line 415: `self._transcription_queue = TranscriptionQueue()` — no
`persist_path` argument. All enqueued jobs are lost on backend restart. The
persistence code is fully implemented and tested but unused in production.

**Recommendation:** pass `persist_path=self.store.data_dir / "transcription_queue.ndjson"`
in `BackendService.__init__`, mirroring the pattern used for `EventReplayManager`
(line 392 in `service.py`).

---

## Finding 7 (gap) — State machine: illegal transitions not guarded

`mark_completed()` and `mark_failed()` accept a job in *any* status — including
`pending` (never processed), `cancelled`, or even a second call on an already
`completed` job. The lock prevents data races but the state machine allows:

- `pending → completed` (skip processing entirely)
- `cancelled → completed` (resurrect a cancelled job)
- `completed → completed` (idempotent but silent overwrite of `result`)

Only `cancel()` explicitly checks its precondition. The other transitions are
unchecked.

**Risk:** low — only the local backend calls these methods in normal flow. Could
cause confusing audit logs or test edge-case failures.

**Recommendation:** add a guard in `mark_completed` / `mark_failed`:

```python
if job.status not in (STATUS_PENDING, STATUS_PROCESSING):
    return False  # or raise
```

---

## Test Coverage

`KrabEar/tests/test_transcription_queue.py` — **674 lines, 8 test classes, ~50 test methods**.

| Area | Covered? |
|---|---|
| Job init / validation | Yes |
| Priority ordering + FIFO | Yes |
| `process_next` in/out of lock | Yes |
| Concurrent `enqueue` + `process_next` | Yes (thread stress) |
| `cancel` on pending / processing / completed | Yes |
| `mark_completed` / `mark_failed` | Yes |
| `peek` | Yes |
| IPC handlers (`handle_*`) | Yes |
| Persistence: enqueue, reload, cancel, corruption | Yes |
| Concurrent `_save` | Yes |
| Illegal state transitions (e.g., cancel→completed) | **No** |
| `mark_completed` on already-completed job | **No** |
| Job leak after process crash (stale `processing`) | **No** |
| Queue size bound / prune | **No** |
| `process_next` never called / dead-code path | **No** |

Coverage of the implemented surface is good. The gaps match the design risks
identified above.

---

## Action Items (priority order)

| # | Severity | Action |
|---|---|---|
| 1 | Medium | Wire `process_next()` to a worker, or expose as IPC + document the pull model |
| 2 | Medium | Add stale-`processing` watchdog / `processing_timeout_sec` |
| 3 | Low | Enable persistence in production (`persist_path` in `service.py`) |
| 4 | Low | Add `prune(max_age_sec)` for terminal-job cleanup |
| 5 | Low | Guard illegal state transitions in `mark_completed` / `mark_failed` |
| 6 | Low | Return `reason` field in `handle_cancel` when already `processing` |
