# Wave 977 Audit: `backend/error_reporter.py`

**Date:** 2026-05-26  
**File:** `KrabEar/backend/error_reporter.py` (161 lines)  
**Tests:** `KrabEar/tests/test_error_reporter.py` (424 lines, 30 test methods)

---

## Summary

`ErrorReporter` is a thin ring-buffer aggregator for per-component error counts and
recent-error retrieval. It is **complementary** — not redundant — to `ErrorBus`
(Phase B structured error surfacing). Five findings follow, one of which is a
data-coherence bug.

---

## Finding 1 — TOCTOU race: `total_in_buffer` read outside the lock (LOW)

`handle_get_error_report` (line 155) reads `len(self._buffer)` **after** releasing
the lock that was held inside `get_recent_errors`. Between the two calls a concurrent
`report_error` or `clear()` can modify the deque, so `total_in_buffer` may not match
the length of the returned `errors` list.

```python
# service.py wires this handler — called from any IPC thread
errors = self.get_recent_errors(limit=limit)   # acquires + releases _lock
return {
    "errors": [e.to_dict() for e in errors],
    "total_in_buffer": len(self._buffer),      # _lock NOT held here — race
}
```

**Fix:** snapshot `len(self._buffer)` inside the same lock in `get_recent_errors`
and return it as a tuple, or read it under a second `with self._lock` block.

---

## Finding 2 — PII: error `message` and `context` may contain transcript text (MEDIUM)

`report_error` stores `message` and `context` verbatim (lines 76–77). Both
`get_error_report` and `get_error_stats` IPC methods expose this to any connected
IPC client, and any call with `exc` also mirrors the message to Sentry. There is
no scrubbing, truncation, or privacy-mode guard.

The `context` dict is particularly high-risk: callers can (and do in other parts of
the codebase) pass raw transcript fragments as context values for debugging.

**Impact:** transcript text can leave the device via Sentry breadcrumbs or IPC
clients if the buffer is queried after an STT/translation error.

**Fix:** either (a) enforce a max-length cap on `message` (e.g., 512 chars) and
disallow transcript-bearing context keys, or (b) gate `get_error_report` behind the
existing `RequestSigner` when `IPC_SIGNING_ENABLED=True`.

---

## Finding 3 — `ErrorReporter` is effectively write-only (MEDIUM)

`_error_reporter.report_error()` is **never called** anywhere in `service.py` or
any other backend module. Only the two IPC read-handlers
(`get_error_report` / `get_error_stats`) are wired. Every real error flows
exclusively through `ErrorBus._push_error` / `error_bus.push(KrabError(...))`.

**Result:** `get_error_report` and `get_error_stats` always return an empty buffer
in production. The ring-buffer exists, is bounded, is thread-safe — but carries
zero data.

**Fix options:**
- Feed `ErrorBus.push()` into `ErrorReporter.report_error()` as a tap, or
- Remove the two IPC methods and `ErrorReporter` entirely (overlap fully covered by
  `list_recent_errors` / `clear_recent_errors` which read `ErrorBus`), or
- Keep as a separate low-level channel and document the split clearly.

---

## Finding 4 — `resolve_error` index semantics are fragile under concurrent writes (LOW)

`resolve_error(index)` takes a positional index into the deque as it exists at the
moment of the call. Between the client calling `get_error_report` (which reverses
the list) and the subsequent `resolve_error(index)` call, new errors can be appended
and old ones evicted, silently marking the wrong record as resolved or returning
`False` unexpectedly.

There are no tests for this race, and no UUID or stable key is attached to
`ErrorRecord` that would allow stable targeting.

---

## Finding 5 — Stack trace capture: full, unbounded, no truncation (LOW)

`report_error` accepts any `message` string and converts it with `str(message)`.
Test `test_record_with_traceback_truncation` confirms a ~3 600-character full
traceback is stored without truncation. Both the in-memory ring and Sentry receive
the full text. There is no max-length enforcement, so repeated tracebacks can inflate
per-record memory and Sentry payload size.

---

## Checklist Coverage

| Question | Result |
|---|---|
| Ring buffer integrity (overflow eviction) | Correct — `deque(maxlen=N)` handles this automatically; counts recomputed from buffer on each `get_error_stats` call, so they naturally stay coherent |
| Thread safety on `add_error` | Safe — `_lock` acquired before `_buffer.append()` |
| Counts coherence (`sum(by_component) == total`) | Always true — both derived from the same buffer snapshot inside one `with self._lock` block in `get_error_stats` |
| Memory bound | Bounded — `deque(maxlen=500)`, default. No secondary unbounded counter dicts |
| PII in messages/context | **YES — finding 2** |
| Test coverage | 30 tests, good path and concurrency coverage; no test for `total_in_buffer` race (finding 1) or `resolve_error` race (finding 4) |
| Stack trace privacy | Full traceback stored, no truncation (finding 5) |
| IPC exposure / auth | Two read-handlers exposed; auth only when `IPC_SIGNING_ENABLED=True` (off by default). Unauth'd local socket access is the only real guard |
| `clear()` atomicity | Atomic — single `deque.clear()` inside `_lock` |
| Overlap with `error_bus` | **Complementary by design, but orphaned in practice** — finding 3 |

---

## Recommendations (priority order)

1. **(MEDIUM)** Wire `ErrorBus.push()` to also call `ErrorReporter.report_error()`,
   or deprecate `get_error_report` / `get_error_stats` IPC methods and remove
   `ErrorReporter`. The current split means two IPC methods return empty data.
2. **(MEDIUM)** Add PII scrubbing: cap `message` at 512 chars, strip or disallow
   transcript-bearing context keys (e.g., `text`, `transcript`, `result`).
3. **(LOW)** Fix `total_in_buffer` TOCTOU by reading under the same lock.
4. **(LOW)** Add a stable `id` field to `ErrorRecord` so `resolve_error` can use
   stable targeting instead of positional index.
5. **(LOW)** Cap `message` length in `report_error` to bound per-record memory and
   Sentry payload size (suggested: 2 048 chars with `...` truncation marker).
