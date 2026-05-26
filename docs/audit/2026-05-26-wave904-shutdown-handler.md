# Audit: GracefulShutdownHandler — Wave 904

**File:** `KrabEar/backend/shutdown_handler.py`
**Date:** 2026-05-26
**Auditor:** Wave 904 snapshot audit
**Scope:** Signal handling, subsystem shutdown order, per-step timeout, idempotency

---

## Summary

`GracefulShutdownHandler` (338 LOC) coordinates orderly backend shutdown: saves state, flushes logs, compacts history, closes the IPC socket, and writes `shutdown_info.json`. Overall quality is **good** — idempotency is correct, error isolation is solid, and atomicity of the info file is implemented. Six findings are documented below (1 high, 2 medium, 3 low/info).

---

## Shutdown sequence

Steps executed in `shutdown()`:

| Step | Action | Subsystem attribute |
|------|--------|---------------------|
| 1 | Save STT vocabulary | `service.vocabulary` |
| 2 | Flush audit log | `service._audit_logger` |
| 3 | Persist usage stats | `service._usage_tracker` |
| 4 | Persist playback stats | `service._playback_tracker` |
| 5 | Compact history (maybe) | `service.store` |
| 6 | Close EventReplayManager | `service._event_replay` |
| 7 | Stop IPC socket server | `service._ipc_server` |
| 8 | Write `shutdown_info.json` | `self._data_dir` |

---

## Findings

### F1 — HIGH: `register()` is never called in production; signal handlers are registered twice and race

**Location:** `service.py:2218–2224` vs `shutdown_handler.py:82–84`

`GracefulShutdownHandler` is instantiated at `service.py:633` with `GracefulShutdownHandler(data_dir=...)` but **`register()` is never called on it**. Instead, `service.py:main()` installs its own inline `_signal_handler` at lines 2223–2224 that only calls `server.stop()` and `service.close()`. The shutdown handler's rich sequence (vocabulary save, audit flush, compaction, etc.) is **never triggered in production**.

Consequence: the shutdown sequence documented in the docstring and tested in `test_shutdown_handler.py` is dead code in production. Only `LLMHttpProbe.stop()` runs (via `service.close()`).

**Recommended fix:** Either wire `handler.register(service)` in `main()` after building the service and ensure it replaces the inline signal handler, or remove `GracefulShutdownHandler.register()` and call `handler.shutdown()` explicitly from `main()`'s `_signal_handler`.

---

### F2 — MEDIUM: No per-step timeout enforcement

**Location:** `shutdown_handler.py:106–158`

Each shutdown step runs synchronously without a wall-clock deadline. If, for example, `maybe_compact()` on a large history file blocks for tens of seconds (file lock contention, slow I/O), the entire backend hangs and `launchd` / `BackendSupervisor` will fire SIGKILL after its own timeout.

The `test_shutdown_handler.py:TestShutdownHandlerTimeoutPerCallback` test uses `sleep(0.05)` and asserts `elapsed < 5.0` — this validates that the test runs fast but does **not** enforce any timeout within the implementation.

**Recommended fix:** Wrap each step in a `threading.Thread` + `join(timeout=N)` guard (e.g. 2 s per step), or use `concurrent.futures.ThreadPoolExecutor` with a per-future timeout. Steps that exceed the deadline should be counted as errors (`clean = False`) with a `{step}: timeout` entry in `errors`.

---

### F3 — MEDIUM: `get_shutdown_status()` `shutdown_in_progress` field is misleading

**Location:** `shutdown_handler.py:196–205`

The `shutdown_in_progress` key is documented as returning `True` if shutdown is ongoing, but the implementation returns `False` before shutdown starts **and** `False` after it completes. The field is only `True` during the window when `_shutdown_started=True` but `_shutdown_done` is not yet set — however the computation inside the lock incorrectly uses `_last_shutdown_time is None` as a proxy, which also matches the pre-shutdown state. In practice the field is unreliable and cannot be distinguished from "not started yet".

**Recommended fix:** Track state explicitly with a tri-state enum or two booleans (`_started`, `_done`) and return a correct value:

```python
"shutdown_in_progress": self._shutdown_started and not self._shutdown_done.is_set()
```

---

### F4 — LOW: Signal handler runs `shutdown()` synchronously on the signal thread

**Location:** `shutdown_handler.py:334–337`

POSIX signal handlers in CPython must be extremely short — they run on the main thread and can interrupt any `threading.Lock` acquisition in progress. `shutdown()` acquires `self._lock` and then calls multiple I/O operations. While CPython's GIL makes this *mostly* safe, acquiring a non-reentrant `threading.Lock` from a signal handler can deadlock if the main thread already holds the lock.

**Recommended fix:** In `_signal_handler`, set a flag and wake a background thread to call `shutdown()`:

```python
def _signal_handler(self, signum: int, frame: Any) -> None:
    threading.Thread(target=self.shutdown, daemon=True).start()
```

This is the standard pattern for signal-safe graceful shutdown in Python.

---

### F5 — LOW: EventReplayManager step (step 6) is a W829 stub without a matching `close()` method guarantee

**Location:** `shutdown_handler.py:262–270`

The comment `# W829 MEDIUM-1` suggests this step was added to address a specific finding, but `EventReplayManager` in `backend/event_replay.py` may not implement a `close()` method (the step uses `getattr(replay, "close", None)` defensively). If `EventReplayManager` is replaced or its API changes, the step silently becomes a no-op with no log message, and un-flushed replay events are lost.

**Recommended fix:** Add `close()` to `EventReplayManager`'s interface (or a `Protocol`), and emit a warning log when the method is absent.

---

### F6 — INFO: `_save_vocabulary` does a redundant round-trip

**Location:** `shutdown_handler.py:212–219`

```python
words = vocab.load()   # reads from disk
vocab.save(words)      # writes same data back
```

`VocabularyStore.load()` reads from disk, which means the step is a read + immediate rewrite of the same data. If `VocabularyStore` has a dirty-tracking mechanism (or an in-memory cache separate from the disk version), the correct call would be `vocab.save(vocab._words)` or `vocab.flush()`. The current pattern is safe but wasteful and could re-encode a stale disk copy if in-memory modifications were not persisted to the store's internal cache.

**Recommended fix:** If `VocabularyStore` exposes a `flush()` or `save_current()` method, prefer it. Otherwise document that the round-trip is intentional.

---

## Positive observations

- **Idempotency:** The `_shutdown_started` flag protected by `self._lock` is correct. Concurrent calls from multiple threads (including the `_signal_handler`) execute exactly one shutdown sequence.
- **Error isolation:** Each step runs inside its own `try/except Exception`. A failure in step 1 does not abort steps 2–7.
- **Atomic file write:** `_persist()` writes to a `.tmp` path and renames atomically via `Path.replace()`, preventing partial JSON corruption on crash mid-write.
- **Test coverage:** `test_shutdown_handler.py` (751 lines, 15 test classes) covers happy path, missing attributes, concurrent calls, error propagation, and step order. `test_shutdown_handler_deep.py` provides additional deep coverage.
- **`service` is stored under lock:** `register()` stores `self._service` inside `self._lock`, and `shutdown()` reads it under the same lock, preventing a TOCTOU race if `register()` is called concurrently with an early signal.

---

## Severity summary

| ID | Severity | Area |
|----|----------|------|
| F1 | HIGH | `register()` not called in production — shutdown steps never run |
| F2 | MEDIUM | No per-step timeout; slow I/O can hang shutdown indefinitely |
| F3 | MEDIUM | `shutdown_in_progress` field is semantically incorrect |
| F4 | LOW | Synchronous `shutdown()` from signal handler risks lock deadlock |
| F5 | LOW | `EventReplayManager.close()` presence is not guaranteed by interface |
| F6 | INFO | Vocabulary round-trip (`load()` then `save()`) is wasteful |
