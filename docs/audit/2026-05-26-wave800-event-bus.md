# Wave 800 — EventBus API & SSE Streaming Audit

**Date:** 2026-05-26
**Scope:** `KrabEar/backend/event_bus.py` + all emit sites + SSE/WS consumers
**Goal:** Surface issues with subscriber leaks, SSE backpressure, thread safety, and
         untyped vs typed emit usage (follow-up to W787 contracts audit).

---

## Summary

| Metric | Value |
|--------|-------|
| EventBus implementation | 131 lines, in-process only |
| Subscriber queue maxsize | 64 events |
| SSE keepalive interval | 15 s |
| WS heartbeat interval | 30 s |
| Untyped `emit()` production call sites | 17 |
| Typed `emit_typed()` production call sites | 6 |
| SSE endpoints | 1 (`GET /v1/events`) |
| WebSocket endpoints | 1 (`/ws/events`) |
| `subscriber_count()` callers (production) | 0 |

---

## Implementation — What's There

### Core API (`event_bus.py`)

```
EventBus
  subscribe()        → queue.Queue  (maxsize=64)
  unsubscribe(q)     → removes queue from list
  emit(str, dict)    → puts event dict into all active queues (non-blocking)
  emit_typed(EventType, BaseModel)  → calls emit() after Pydantic serialization
  subscriber_count() → int  (thread-safe count)

sse_stream(bus, event_filter=None)  → Iterator[str]
  subscribes on entry, unsubscribes in finally
  keepalive every 15 s on queue.Empty
```

### Thread Safety Design

- `_lock: threading.Lock()` guards `_subscribers` list for all mutations.
- `emit()` copies the list before iterating (`active = list(self._subscribers)`) so
  concurrent `unsubscribe()` during emit does not cause a mid-iteration removal.
- This pattern is correct and race-free.

### Backpressure Design

- Each subscriber queue is bounded at 64 events.
- `emit()` uses `q.put_nowait()` — never blocks the emitter thread.
- If a queue is full, the event is silently dropped and a `WARNING` log is emitted.
- No per-subscriber slow-path eviction (oldest-event-drop) is implemented.
- No counter or metric is exposed for dropped events beyond the warning log.

### Shutdown Sentinel

- `sse_stream()` breaks its loop when `event is None`.
- **Issue:** `EventBus` has no `close()` or `broadcast_shutdown()` method. The `None`
  sentinel is documented but never sent by any production code path. `GracefulShutdownHandler`
  does not call any event bus teardown. SSE and WS connections will hang at `q.get(timeout=…)`
  until the OS kills the process.

---

## Emit Site Inventory

### Typed emit (`emit_typed`) — 6 sites

All 6 typed sites are in `recording_core_service.py` and `live_subs_service.py`.

| EventType | File | Line | Notes |
|-----------|------|------|-------|
| `STT_PARTIAL` | `recording_core_service.py` | 674 | Pydantic-validated |
| `STT_FINAL` | `recording_core_service.py` | 1218 | Pydantic-validated |
| `STT_FAILED` | `recording_core_service.py` | 1042 | Pydantic-validated |
| `TRANSLATION_COMPLETED` | `recording_core_service.py` | 1075 | Pydantic-validated |
| `TRANSLATION_FAILED` | `recording_core_service.py` | 1085 | Pydantic-validated |
| `LIVE_SUBS_RESULT` | `live_subs_service.py` | 191 | Pydantic-validated |

### Untyped emit (`emit`) — 17 sites (no schema enforcement)

#### Priority 1 — High-frequency / consumer-facing

| String key | File | Line | Notes |
|-----------|------|------|-------|
| `"realtime.partial_transcript"` | `realtime_partial.py` | 169 | Via `_REALTIME_PARTIAL_TYPE` const; guarded by try/except |
| `"realtime.final_transcript"` | `recording_core_service.py` | 1227 | Try/except with debug log on failure |
| `"recording.audio_level"` | `service.py` | 180 | High-frequency; fires every audio callback |
| `"app.status"` | `recording_core_service.py` (368), `obsidian_sync.py` (136, 155, 184, 200) | multiple | 5 distinct call sites; free-form dict shape |
| `"krab_error"` | `error_bus.py` | 189 | Error reporting; unguarded |

#### Priority 2 — Operational / monitoring

| String key | File | Line | Notes |
|-----------|------|------|-------|
| `"rewriter_recovered"` | `llm_probe.py` | 253 | Guarded — try/except + warning log |
| `"preset.changed"` | `settings_service.py` | 360 | Via module alias `_ebus.bus.emit(...)` |
| `"bulk_reprocess_progress"` | `bulk_reprocess.py` | 119 | Per-item progress |
| `"playback.seek"` | `bookmarks.py` | 266 | Unguarded |
| `"recording.silence_detected"` | `realtime_silence_filter.py` | 159 | Guarded by `try/except pass` |

#### Priority 3 — Disk / infra

| String key | File | Line | Notes |
|-----------|------|------|-------|
| `f"disk.{level}"` → `"disk.warning"` / `"disk.critical"` | `disk_monitor.py` | 228 | Dynamic key |
| `"disk.history_large"` | `disk_monitor.py` | 263 | |
| `"disk.auto_cleanup_requested"` | `disk_monitor.py` | 340 | |

#### Intentionally dynamic / passthrough

| Pattern | File | Line | Notes |
|---------|------|------|-------|
| `bus.emit(event_type, event_data)` | `vg_ws_client.py` | 57 | Forwards VG WS frames verbatim; event_type from wire protocol; typing not applicable |

---

## Issues Found

### ISSUE-1 — No `EventBus.close()` — Orphan Subscribers at Shutdown

**Severity:** LOW (process exits anyway, no memory growth)
**File:** `event_bus.py`
**Detail:**

The docstring for `subscribe()` says "None in queue means shutdown signal", but
`EventBus` has no `close()` method that broadcasts `None` to all queued subscribers.
`GracefulShutdownHandler.shutdown()` (steps 1–7, `shutdown_handler.py`) does not touch
the event bus at all.

When the backend exits, SSE (`sse_stream()`) and WS (`_handle_ws_connection()`) threads
are blocked at `q.get(timeout=15)` / `q.get(timeout=5)`. They will not receive a clean
exit signal; they hang until the OS terminates the process or the timeout fires and they
loop again. The threads are daemon threads (Flask/Werkzeug owns them), so this does not
block shutdown in practice, but it means SSE clients never receive a proper close frame.

**Recommended fix:**
```python
def close(self) -> None:
    """Broadcast shutdown sentinel to all subscribers and clear the list."""
    with self._lock:
        for q in self._subscribers:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        self._subscribers.clear()
```
Call from `GracefulShutdownHandler.shutdown()` before closing the IPC socket.

---

### ISSUE-2 — Backpressure: Drop-only, No Slow-Subscriber Eviction

**Severity:** LOW (single-process, localhost clients only)
**File:** `event_bus.py`, `service.py`
**Detail:**

`recording.audio_level` is emitted from the audio callback in `service.py:180` on every
PCM buffer. At 16 kHz / 512-frame chunks this is ~31 events/second. A slow SSE consumer
(e.g., a suspended browser tab) will fill its 64-event queue in ~2 s and then drop
every subsequent audio-level event, producing the `WARNING` log continuously during
recording.

The drop is safe — it is the correct behavior for a real-time signal. However:
- The warning log is emitted once per dropped event (potentially 31 log lines/sec).
- There is no debounce or rate-limit on the warning log itself.

**Recommended fix (low effort):**
Use a `dropped_logged` flag or log only when `dropped > 0 and dropped % 10 == 0` to
prevent log spam. Alternatively, suppress the warning for known high-frequency event
types (`recording.audio_level`).

---

### ISSUE-3 — `subscriber_count()` Never Used in Production Monitoring

**Severity:** INFO
**File:** `event_bus.py:87`
**Detail:**

`EventBus.subscriber_count()` is used only in tests (`test_event_bus.py`,
`test_ws_streaming.py`) to assert cleanup. No production path exposes it via metrics,
diagnostics IPC (`get_diagnostics`), or health checks. If subscriber accumulation were
to happen (e.g., due to a future code change that skips `unsubscribe`), there is no
production signal.

**Recommended fix:** Add `"event_bus_subscribers": bus.subscriber_count()` to the
`system` section of `handle_get_diagnostics` in `HealthCheckService`.

---

### ISSUE-4 — 17 Untyped Sites: `"app.status"` Has 5 Callers with Inconsistent Schemas

**Severity:** LOW
**File:** `recording_core_service.py`, `obsidian_sync.py`
**Detail:**

`"app.status"` is emitted from 5 locations:
- `recording_core_service.py:368` — `{"status": str, "recording_id": str}`
- `obsidian_sync.py:136` — `{"message": str, "sync_count": int}`
- `obsidian_sync.py:155` — `{"message": str, "file": str}`
- `obsidian_sync.py:184` — `{"message": str}`
- `obsidian_sync.py:200` — `{"message": str, "error": str}`

All share the `"app.status"` key but have incompatible payload shapes. SSE consumers
cannot safely parse this without defensive key-access. This is the highest-value
candidate for schema enforcement from the untyped set.

**Recommended fix:** Define `AppStatusEvent(BaseModel)` in `contracts/registry.py` with
optional fields covering all shapes, add `APP_STATUS` to `EventType`, and migrate all
5 sites to `emit_typed`.

---

### ISSUE-5 — `realtime_silence_filter.py` Uses `except: pass` on Emit

**Severity:** INFO
**File:** `realtime_silence_filter.py:156–167`
**Detail:**

The emit call is wrapped in a broad `except Exception: pass` with no logging. If
`event_bus.emit()` raises (unlikely but possible), the failure is silently swallowed
with no breadcrumb. Pattern inconsistent with `realtime_partial.py` which uses
`self._log_error(...)` on emit failure.

**Recommended fix:** Replace bare `pass` with a `logger.debug("silence emit failed", exc_info=True)`.

---

### ISSUE-6 — WS Endpoint Exception Swallows Connection Errors

**Severity:** INFO
**File:** `rest_server.py:1047–1048`
**Detail:**

```python
except Exception:
    logger.debug("WS /ws/events: соединение прервано")
```

The outer `except Exception` in `_handle_ws_connection` logs a debug message with no
exc_info, making it impossible to distinguish a clean client disconnect from a Python
exception in the handler loop. WS send errors are caught separately (break on Exception
at line 1046) with no log.

**Recommended fix:** Add `exc_info=True` to the outer except, or use `logger.exception`
at WARNING level to surface unexpected errors without spamming debug on normal disconnects.

---

## Thread Safety Assessment

**Result: CORRECT**

| Concern | Status | Detail |
|---------|--------|--------|
| `_subscribers` list mutation vs emit | Safe | `emit()` snapshots list with `list(...)` before iterating |
| Concurrent `subscribe` + `emit` | Safe | Both acquire `_lock` |
| Concurrent `unsubscribe` + `emit` | Safe | Snapshot means removal doesn't affect current iteration |
| `queue.put_nowait` from multiple threads | Safe | Python `queue.Queue` is thread-safe by design |
| `subscriber_count()` | Safe | Acquires `_lock` |

No races identified.

---

## SSE Backpressure Assessment

**Result: ACCEPTABLE FOR SINGLE-PROCESS USE**

| Concern | Status | Detail |
|---------|--------|--------|
| Emitter blocking on slow consumer | Not possible | `put_nowait` — never blocks |
| Queue fill-up → event loss | Yes, by design | Drop + WARNING log |
| Warning log spam on audio_level | Issue-2 | ~31 warnings/sec during recording with slow consumer |
| Keepalive prevents proxy timeouts | Yes | 15 s comment sent on `queue.Empty` |
| WS heartbeat | Yes | 30 s ping frame |
| Shutdown sentinel broadcast | Missing | Issue-1 |

---

## Recommendations (Priority Order)

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 1 | Add `EventBus.close()` + call from `GracefulShutdownHandler` | Small | Correct SSE teardown on shutdown |
| 2 | Suppress/debounce drop warning for high-frequency events | Trivial | Eliminates log spam on `recording.audio_level` |
| 3 | Expose `subscriber_count()` in `get_diagnostics` response | Trivial | Production visibility |
| 4 | Define `AppStatusEvent` and migrate 5 `"app.status"` sites to typed emit | Medium | Schema enforcement for most-polymorphic event |
| 5 | Replace `except: pass` in `realtime_silence_filter.py:167` with debug log | Trivial | Observability parity |
| 6 | Add `exc_info=True` to WS outer except | Trivial | Debug parity |

Items 2, 3, 5, 6 are one-liner fixes suitable for a single cleanup PR.
Item 1 is a ~15 LOC addition (`EventBus.close()` + `shutdown_handler.py` step 7.5).
Item 4 is the only medium-effort item; defer until the contracts typing sweep (W787 action).

---

## Cross-references

- **W787** (`2026-05-26-wave787-contracts-audit.md`) — full untyped emit site list + typed migration roadmap
- **`backend/event_bus.py`** — implementation (131 lines)
- **`backend/rest_server.py:966–1051`** — SSE + WS consumer endpoints
- **`backend/shutdown_handler.py`** — graceful shutdown (does not touch EventBus)
- **`backend/realtime_partial.py:169`** — highest-frequency emitter
- **`backend/service.py:180`** — audio-level emitter (31 events/sec during recording)
