# Wave 846 — ErrorBus audit: dedupe, ring buffer, Sentry routing, WarnBatcher

**Date:** 2026-05-26
**File:** `KrabEar/backend/error_bus.py`
**Companion:** `KrabEar/backend/error_codes.py`
**Auditor:** Wave 846 sub-agent

---

## Summary

`error_bus.py` is 248 lines across three classes: `KrabError` (Pydantic model), `WarnBatcher`
(batching accumulator), and `ErrorBus` (main pusher). The module is structurally sound with
correct thread-safety throughout. Four findings were identified: one bug-level (Sentry level
string mismatch), two design gaps (no periodic background flush, no shutdown flush), and one
hardening note (degenerate context dict handling).

**Findings: 4**

---

## Finding 1 — BUG: wrong Sentry level string `"warn"` (should be `"warning"`)

**Severity:** Bug (silent data quality issue)
**Location:** `error_bus.py:112` (`WarnBatcher._flush_locked`)

```python
self._sentry.capture_message(
    summary,
    level="warn",   # <-- BUG
    ...
)
```

The Sentry SDK's `LogLevelStr` type (from `sentry_sdk._types`) is defined as:

```python
LogLevelStr = Literal["fatal", "critical", "error", "warning", "info", "debug"]
```

`"warn"` is **not** in the accepted set. Sentry SDK `Scope.update_from_kwargs` passes the
level verbatim into the event dict. Depending on SDK version, this either silently sends events
with an unknown level (they appear as "info" in the Sentry UI) or raises a validation warning.

The `_route_to_sentry` immediate path (line 244) correctly uses `err.severity` which is typed
as `Literal["info", "warn", "error", "critical"]`. This means `warn`-severity errors that
*do* reach the immediate path (they don't — `warn` routes to `WarnBatcher`) would also pass
`"warn"` to Sentry. However that code path is currently unreachable for `warn` errors.

**Fix:** Change `level="warn"` → `level="warning"` on line 112 of `WarnBatcher._flush_locked`.

---

## Finding 2 — DESIGN GAP: WarnBatcher window flush is not timer-driven

**Severity:** Design gap (warn batches can silently expire undelivered)
**Location:** `error_bus.py:82–99` (`WarnBatcher.add`)

The window-flush condition checks `(now - self._first_seen[code]) >= self._window` inside
`add()`. This means the window flush only fires when a *new error of the same code* arrives
after `window` seconds. If an error code fires, say, exactly 9 times (below `batch_size=10`)
and then goes quiet, its accumulated batch **never gets flushed to Sentry** because no
subsequent call to `add()` triggers the window check.

**Production scenario:** `rewriter.timeout` fires 7 times in a burst, then the user fixes
LM Studio. The 7 accumulated errors stay in `_buffer["rewriter.timeout"]` indefinitely — Sentry
never receives them. Only a process restart would discard them (silently).

There is no background timer thread, no `atexit` hook, and no `shutdown` / `flush_all` method
on `WarnBatcher`. The docstring states flush happens "when … window seconds have elapsed since
first arrival" but does not document that this requires a subsequent `add()` call to be the
actual trigger.

**Fix options (in increasing robustness order):**
1. Add a `flush_all()` method that flushes all pending codes; call it from `BackendService`
   shutdown path (`shutdown_handler.py`).
2. Add a `threading.Timer` in `WarnBatcher.__init__` that fires every `window` seconds and
   calls `_flush_pending_locked()` (flush any code whose window has elapsed).
3. Option 2 + option 1 as belt-and-suspenders.

Option 1 is the minimum viable fix. Timer-driven (option 2) is more correct.

---

## Finding 3 — DESIGN GAP: `ErrorBus.clear()` does not flush pending WarnBatcher state

**Severity:** Design gap (silent discard on diagnostics clear)
**Location:** `error_bus.py:199–205` (`ErrorBus.clear`)

```python
def clear(self) -> int:
    with self._lock:
        count = len(self._ring)
        self._ring.clear()
        self._last_emitted.clear()
    return count
```

`clear()` resets the ring buffer and dedupe state but does **not** touch `self._warn_batcher`.
Consequence: after `clear_recent_errors` IPC call, pending warn batches in `_warn_batcher._buffer`
survive untouched while `_last_emitted` is empty — the next `push()` for any previously-cleared
code will bypass dedupe (correct for the ring buffer intent) but the WarnBatcher will still hold
its old partially-accumulated batch under that code. This creates a subtle split: the ring shows
a fresh error, but the batch count at Sentry flush time reflects carries from before the clear.

Additionally: the `_last_emitted` clear means a high-frequency code (e.g. `audio.buffer_overflow`
with `dedupe_seconds=5`) would fire again immediately after `clear()` — this is probably the
intended behaviour (reset diagnostics view), but the interaction with WarnBatcher is undocumented.

**Fix:** Document this explicitly OR add `self._warn_batcher._buffer.clear(); self._warn_batcher._first_seen.clear()`
inside `clear()` (requires WarnBatcher to expose a `reset()` method or accept that `_lock` must
be held).

---

## Finding 4 — HARDENING: `context` dict keys not sanitised before Sentry `extras`

**Severity:** Low / hardening note
**Location:** `error_bus.py:114`, `error_bus.py:246`

Both `WarnBatcher._flush_locked` and `ErrorBus._route_to_sentry` pass `err.context` (or
`**latest.context`) as `extras=` to `sentry_sdk.capture_message`. The `KrabError` model
declares `context: dict` without type constraints on values.

Sentry `extras` serialises values via `repr()` for non-JSON-serialisable types. If a call site
accidentally passes a non-serialisable object (e.g. an `Exception`, a numpy array, a file handle)
inside `context`, Sentry silently uses `repr()`. This is generally harmless but can produce
unreadably large `extras` payloads for complex objects and can cause the Sentry event to be
dropped if it exceeds the 200 KB payload limit.

No call sites in the current codebase appear to pass complex objects — all observed `context`
dicts contain strings, ints, and booleans. This is a hardening note for future call sites.

**Fix:** Annotate `context: dict[str, str | int | float | bool | None]` in `KrabError` to
enforce at the Pydantic validation layer, or add a `_sanitise_context` helper.

---

## Correctness review: items verified as correct

### Dedupe logic

`_dedupe_window_for` correctly handles both registry shapes: flat `{code: seconds}` (test
fixtures) and canonical `_Entry` TypedDict from `error_codes.py` (production). Fallback to
`default_dedupe_window_sec` is correct. The dedupe check uses `time.monotonic()` — immune to
wall-clock jumps.

The `push()` method releases `_lock` **before** calling `event_bus.emit()` and
`_route_to_sentry()`. This correctly avoids dead-lock if EventBus callbacks re-enter `push()`.

### Ring buffer overflow

`deque(maxlen=ring_buffer_size)` with default `ring_buffer_size=200`. `deque` with `maxlen`
drops the leftmost (oldest) element on overflow — this is Python's documented behaviour and
exactly the correct semantics for a ring buffer. No off-by-one or race condition.

`list_recent(limit)` copies under the lock; the slice `items[-limit:]` is correct for
"most recent N" semantics. When `limit >= len(items)`, it returns the full list — correct.

### Sentry tier routing

| Severity | Routed to |
|----------|-----------|
| `info`   | Dropped (never sent) |
| `warn`   | `WarnBatcher.add()` — batch by code; flush at `batch_size` or `window` |
| `error`  | Immediate `capture_message(level="error")` |
| `critical` | Immediate `capture_message(level="critical")` |

The routing is logically correct. `sentry_client is None` guard prevents AttributeError when
Sentry is not configured. `_warn_batcher is None` guard on line 238 is redundant (batcher is
only `None` when `sentry_client is None`, which is already guarded on line 235) but harmless.

### WarnBatcher thread-safety

`WarnBatcher` uses its own `_lock` (independent of `ErrorBus._lock`). The order is: `ErrorBus`
releases its lock → calls `_route_to_sentry` → calls `_warn_batcher.add()` → acquires
`WarnBatcher._lock`. There is no shared lock between the two classes, so no dead-lock risk.
`_flush_locked` is only called while holding `WarnBatcher._lock`, correctly named.

### `_last_emitted` memory growth

`_last_emitted: dict[str, float]` is never pruned. With 57 registered codes and at most one
entry per code, the dict stays bounded at 57 entries regardless of call volume. Not a leak.

---

## Error code registry cross-check

`error_codes.py` defines 57 codes as of 2026-05-26 (Wave 306 last addition:
`rewriter.lm_studio_stream_gpu_lost`). All entries contain the required `_Entry` keys:
`user_msg_ru`, `actionable`, `action_id`, `action_label`, `severity`, `dedupe_seconds`.

`ErrorBus._dedupe_window_for` extracts `entry["dedupe_seconds"]` via `entry.get(…)` with
fallback — this is safe against missing keys but the TypedDict definition means all canonical
entries will have the key. No drift between code and registry structure detected.

Severity values in the registry: `info`, `warn`, `error`, `critical` — all match the
`Severity` Literal in `error_bus.py`.

---

## Action items

| Priority | Finding | Recommended fix |
|----------|---------|----------------|
| HIGH | F1: `level="warn"` → should be `"warning"` | 1-line fix in `WarnBatcher._flush_locked` |
| MED | F2: No timer-driven window flush → batches can be silently lost | Add `flush_all()` + call from shutdown; optionally add `threading.Timer` |
| LOW | F3: `clear()` leaves WarnBatcher state inconsistent | Add `WarnBatcher.reset()` and call from `ErrorBus.clear()` |
| LOW | F4: `context` dict type unconstrained | Narrow `context` type annotation or add sanitiser |
