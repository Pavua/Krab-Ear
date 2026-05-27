# Wave 975 — GracefulShutdownHandler Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/shutdown_handler.py`  
**Tests reviewed:** `KrabEar/tests/test_shutdown_handler.py`, `KrabEar/tests/test_shutdown_handler_deep.py`

---

## Summary

`GracefulShutdownHandler` is a straightforward, well-structured module with good exception isolation and idempotency. However, a critical integration gap means the handler is **never actually wired in production**, and two structural gaps (no per-hook timeout, no force-kill fallback) could cause indefinite hangs if any hook blocks. The IPC socket is closed after data flushing, which is the correct ordering, but a wrong ordering of signal registration in `main()` overrides the handler entirely.

---

## Findings

### CRITICAL — Handler never registered: `main()` overwrites SIGTERM/SIGINT

**Location:** `KrabEar/backend/service.py` lines 3858–3864  
**Severity:** HIGH

`GracefulShutdownHandler` is instantiated inside `BackendService.__init__` (line 553) but `register(service)` is **never called**. The `main()` entrypoint registers its own bare `_signal_handler` that only calls `server.stop()` and `service.close()` — `service.close()` stops only the `LLMHttpProbe` thread. None of the shutdown steps (vocabulary save, audit log flush, usage stats, playback stats, history compaction) are executed on SIGTERM/SIGINT in production.

The six-step sequence in `GracefulShutdownHandler.shutdown()` is tested in isolation but dead in the running process. To activate it, `main()` would need:

```python
service._shutdown_handler.register(service)
```

called after `build_service()` and before `server.serve_forever()`, and the duplicate `signal.signal(SIGTERM/SIGINT, ...)` lines in `main()` removed or consolidated.

**Risk:** Vocabulary words, usage stats, playback stats, and the audit log are silently dropped on every SIGTERM (normal launchd stop). History compaction is also skipped, causing gradual NDJSON bloat.

---

### HIGH — No per-hook timeout; any blocking hook hangs shutdown indefinitely

**Location:** `KrabEar/backend/shutdown_handler.py` lines 106–151  
**Severity:** HIGH

Each of the six steps (`_save_vocabulary`, `_flush_audit_log`, `_save_usage_stats`, `_save_playback_stats`, `_maybe_compact_history`, `_close_socket`) is called synchronously with no timeout. If any hook blocks (e.g., `maybe_compact()` performs large file I/O, or `_ipc_server.stop()` waits for in-flight connections to drain), the shutdown sequence hangs forever.

The `TestCallbackTimeout` test in `test_shutdown_handler_deep.py` (line 388) explicitly acknowledges this gap: *"GracefulShutdownHandler currently does NOT implement per-step timeout internally."* The test only verifies a 50 ms sleep finishes in 2 s, which is not a timeout enforcement — it is a tautology.

**Mitigation path:** Wrap each step in a `threading.Timer`-based timeout or run steps in daemon threads joined with `thread.join(timeout=N)`.

---

### HIGH — No force-kill fallback; graceful shutdown can delay process exit indefinitely

**Location:** `KrabEar/backend/shutdown_handler.py` — entire `shutdown()` method  
**Severity:** HIGH

After `shutdown()` completes all six steps (or hangs in one of them), the process does not explicitly call `sys.exit()` or `os._exit()`. The expectation is that `server.serve_forever()` returns naturally. But if `_close_socket` silently fails or if the `serve_forever()` loop does not observe the socket closure, the process never exits.

There is no watchdog timer (e.g., `threading.Timer(30, os._exit, [1])`) started at the beginning of `shutdown()` to guarantee that the process terminates within a maximum wall-clock bound even if individual steps hang or `serve_forever()` does not return. Systemd/launchd will escalate to SIGKILL after its own timeout (typically 30 s for launchd), so this is partially mitigated by the OS — but data loss is possible if the force-kill races with an incomplete flush.

---

### MEDIUM — Socket closed after data flushes: correct ordering, but history compaction races with socket closure

**Location:** `KrabEar/backend/shutdown_handler.py` lines 105–151  
**Severity:** MEDIUM

The ordering is:  
1. vocabulary save  
2. audit log flush  
3. usage stats  
4. playback stats  
5. history compaction (`maybe_compact`)  
6. close IPC socket  

Closing the socket last is correct — no new requests can arrive after step 6. However, step 5 (`maybe_compact`) can write a new compacted `history.ndjson` while the IPC socket is still open. If a concurrent IPC request arrives during compaction and also writes to the store (via `StateStore`), the compaction could race with the writer. `StateStore` uses file locks, so this is low-probability but not impossible under load.

**The recommended safe ordering** would be to close the IPC socket first (step 1), then flush all data stores, so no new writes can arrive during data persistence. The current ordering prioritises data safety by flushing first but accepts the race window.

---

### MEDIUM — `get_shutdown_status()` returns incorrect `shutdown_in_progress`

**Location:** `KrabEar/backend/shutdown_handler.py` lines 188–198  
**Severity:** MEDIUM

The `shutdown_in_progress` field in `get_shutdown_status()` is computed as:

```python
self._shutdown_done.is_set() is False
and self._last_shutdown_time is None
and self._service is not None
```

This condition is `True` only before any shutdown has started **and** a service is registered — not actually "in progress". Once `_shutdown_started` is set to `True` but before `_shutdown_done.is_set()`, the condition still returns `False` because `_last_shutdown_time` is only set after the full sequence completes, so `_last_shutdown_time is None` while shutdown is genuinely in progress — making `shutdown_in_progress` evaluate to `True`. But the in-progress window also includes the "not yet started" state, making the field misleading. The comment in the source acknowledges this: *"Упрощаем: False до завершения, True только после."* The field should use `_shutdown_started` directly.

**Proposed fix:**
```python
"shutdown_in_progress": self._shutdown_started and not self._shutdown_done.is_set()
```

---

### LOW — `_signal_handler` called from signal context; `shutdown()` is not async-signal-safe

**Location:** `KrabEar/backend/shutdown_handler.py` lines 316–319  
**Severity:** LOW

`_signal_handler` calls `self.shutdown()` directly from the signal handler. `shutdown()` acquires a `threading.Lock`, performs file I/O, and calls into multiple service collaborators — none of which are async-signal-safe. On CPython this works reliably in practice because the GIL synchronises signal delivery to the main thread's bytecode boundary, but it is technically undefined behaviour per POSIX and could deadlock if the signal arrives while the main thread holds `self._lock`.

**Safer pattern:** set a `threading.Event` in the signal handler and have the main thread poll or wait on that event to initiate shutdown. This is low-priority given CPython's actual signal delivery model, but worth noting for correctness.

---

## Test Coverage Assessment

Both test files (`test_shutdown_handler.py` — 13 classes, ~45 tests; `test_shutdown_handler_deep.py` — 10 classes, ~22 tests) provide good unit coverage of the handler in isolation:

- Happy path, missing attributes, exception isolation, idempotency, thread safety, signal registration, persistence, atomic file write, ordering (deterministic step sequence verified with `call_order` list).
- The timeout test (Wave 106 / deep test class 7) explicitly documents the absence of per-hook timeout enforcement — correct to flag but not enforce what does not exist.

**Gap:** No integration test verifies that `GracefulShutdownHandler.register(service)` is actually called from `main()`. The critical finding above (handler never registered) would be caught by such a test.

---

## Recommendations

| Priority | Action |
|----------|--------|
| CRITICAL | Call `service._shutdown_handler.register(service)` in `main()` after `build_service()`; remove duplicate `signal.signal()` calls from `main()`. |
| HIGH | Add a `threading.Timer(30, os._exit, [1])` watchdog at the start of `shutdown()` to bound total shutdown time. |
| HIGH | Wrap each step in a per-step timeout (e.g., `threading.Thread(target=step, daemon=True).join(timeout=5)`). |
| MEDIUM | Fix `shutdown_in_progress` to use `self._shutdown_started and not self._shutdown_done.is_set()`. |
| LOW | Move `shutdown()` call out of signal handler into a watcher thread/event. |
