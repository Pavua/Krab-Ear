# Wave 1194 — EventBus Residual Re-Audit

**Date:** 2026-05-26
**Branch:** `audit/event-bus-residual-W1194`
**Scope:** `KrabEar/backend/event_bus.py`, `KrabEar/backend/rest_server.py` (SSE + WS),
           `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/realtime_partial.py`,
           `KrabEar/backend/realtime_silence_filter.py`, `KrabEar/backend/error_bus.py`,
           `KrabEar/backend/shutdown_handler.py`, `KrabEar/contracts/registry.py`,
           all emit sites
**Predecessors:** W800 (`2026-05-26-wave800-event-bus.md`) — 6 issues;
                 W892 (`2026-05-26-wave892-event-bus-deep.md`) — 5 findings + typed migration plan
**Analyst:** W1194 re-audit pass

---

## 1. W892 Finding Status Verification

W892 documented 5 FINDING items plus a `retry:` SSE gap. Current status against live
`codex/krab-ear-v2` (commit `6c900317`):

| W892 Item | Status | Evidence |
|-----------|--------|----------|
| FINDING-1: SSE docstring lists only 2 event types | **OPEN** | `rest_server.py:982` — docstring still shows only `stt.final` and `stt.failed` |
| FINDING-2: SSE endpoint not injectable (no `bus` param) | **OPEN** | `rest_server.py:986` — `events_stream()` still uses module-level singleton directly |
| FINDING-3: No SSE connection count limit | **OPEN** | No `MAX_SSE_CONNECTIONS` guard in `rest_server.py` |
| FINDING-4: WS auth bypass on empty-string API key | **OPEN** | `rest_server.py:1123–1141` unchanged; no doc update |
| FINDING-5: Generator leak on client abort (Flask dev server) | **OPEN** | `event_bus.py:125` — `finally: bus.unsubscribe(q)` is the only cleanup; no docstring note added |
| `retry:` field missing from SSE stream | **OPEN** | `event_bus.py` — no `retry:` line added |
| Drop warning not structured (W892 extension of W800-ISSUE-2) | **OPEN** | `event_bus.py:81` — `logger.warning(...)` still has no `extra={}` |
| Migration plan P1a: `krab_error` → typed | **OPEN** | `error_bus.py:189` still `self._event_bus.emit("krab_error", payload)` |
| Migration plan P1b: `realtime.*` → typed | **OPEN** | `realtime_partial.py:169` still untyped; `EventType` has no `REALTIME_PARTIAL` member |
| Migration plan P1c: `app.status` → typed | **OPEN** | `obsidian_sync.py:136,155,184,200` + `recording_core_service.py:338` all untyped |

**All 5 W892 FINDING items plus all migration plan items remain open. Zero W892
findings were addressed between W892 and this audit.**

W800 ISSUE-5 (`realtime_silence_filter.py` bare `except: pass` on emit) — **still
present** at line 165. W800 ISSUE-6 (WS exception swallowed) — **still present** at
line 1047.

---

## 2. New Findings (W1194)

### FINDING-1 — `privacy_mode_enabled` does not gate `realtime.partial_transcript` SSE emission

**Severity: MEDIUM**
**File:** `KrabEar/backend/recording_core_service.py:171–196`, `KrabEar/backend/realtime_partial.py:168–175`

`privacy_mode_enabled` is checked in `TranslationService` (blocks translation calls)
and in `observability.py` (blocks Sentry init). However, the
`RealtimePartialTranscriber` is started unconditionally by `handle_start_recording()`:

```python
# recording_core_service.py:171
if bool(settings.get("realtime_partial_enabled", True)):
    ...
    self._rt_partial = RealtimePartialTranscriber(...)
    self._rt_partial.start(...)
```

There is **no `privacy_mode_enabled` check** before launching the thread. As a result,
while `privacy_mode_enabled=True` is set:

1. `realtime.partial_transcript` events continue to flow into `EventBus` with full
   transcript text (field `"text": text` at `realtime_partial.py:173`).
2. Any connected SSE subscriber (`GET /v1/events`) receives live transcript text in
   plaintext JSON over the local HTTP port.
3. The corresponding `stt.final` event uses `emit_typed(EventType.STT_FINAL, SttFinal(text=...))`,
   also unconditionally.

The `privacy_mode_enabled` flag therefore only blocks Sentry/translation but not the
local SSE stream. This may be intentional (user controls who reads localhost:5005), but
there is **no documentation of this design decision** and no test asserting the behavior.
Given that privacy mode is advertised as preventing transcript data leaving the local
process, SSE delivery to localhost REST consumers is a gap.

**Recommended fix:** Add `privacy_mode_enabled` check in `handle_start_recording()`:

```python
privacy_mode = bool(settings.get("privacy_mode_enabled", False))
if not privacy_mode and bool(settings.get("realtime_partial_enabled", True)):
    ...start RealtimePartialTranscriber...
```

And add a note in `sse_stream()` docstring that privacy mode does not filter SSE content.
Alternatively, document explicitly that SSE is in-process/localhost-only and privacy
mode intentionally excludes it. **Either fix the gap or document the design decision.**

---

### FINDING-2 — Typed emit ordering asymmetry: `emit_typed` serializes payload via Pydantic but `emit` does not validate — mixed pipeline risks schema drift

**Severity: LOW**
**Files:** `KrabEar/backend/event_bus.py:83–85`, `KrabEar/backend/error_bus.py:189`, `KrabEar/backend/recording_core_service.py:1186`

`emit_typed()` calls `payload.model_dump(mode="json")` which Pydantic-validates the
payload before serialization. `emit()` takes a raw `dict[str, Any]` with no validation.

The consequence is that `realtime.final_transcript` (emitted by `recording_core_service.py:1186`)
and `krab_error` (emitted by `error_bus.py:189`) are the two highest-value events for
Swift consumers, yet both go through the untyped path. The Swift side
(`ErrorActionHandler.swift`, `main+Errors.swift`) manually decodes `krab_error` payloads
using a `KrabErrorPayload` Codable struct.

If a Python-side refactor changes the `KrabError` dict shape (e.g., adds a required
field), the Swift decoder silently fails (returns `nil` from the optional decode) —
there is no compile-time check. The typed path would catch this at the Python emit site
as a `ValidationError`.

This is the W892 P1 migration target confirmed still open. Calling it out here as a
concrete ordering risk (the `KrabError` Pydantic model already exists and matches the
dict shape — migration cost is ~5 lines, not a design change).

**Recommended fix (minimal):** Change `error_bus.py:189`:
```python
# Before:
self._event_bus.emit("krab_error", payload)
# After (KrabError is already a BaseModel):
self._event_bus.emit_typed(EventType.KRAB_ERROR, self._current_error)
```
Requires adding `EventType.KRAB_ERROR = "krab_error"` to `contracts/registry.py` and
`EVENT_SCHEMA_MAP`. **Effort: ~15 min. Risk: zero.**

---

### FINDING-3 — Subscriber leak window: `unsubscribe()` in `sse_stream` finally block races with Flask response teardown

**Severity: LOW**
**File:** `KrabEar/backend/event_bus.py:108–126`, `KrabEar/backend/rest_server.py:983–987`

`sse_stream()` correctly calls `bus.unsubscribe(q)` in the `finally` block. However,
the SSE endpoint wires `sse_stream()` via `stream_with_context()`:

```python
return Response(
    stream_with_context(sse_stream(event_bus, event_filter=event_filter)),
    mimetype="text/event-stream",
    ...
)
```

`stream_with_context` wraps the generator with Flask's application context but does not
guarantee that `GeneratorExit` is delivered to the inner generator on `Response`
teardown. Under Werkzeug (dev server), if the TCP connection drops while the generator
is blocked on `q.get(timeout=15)`, Python does not interrupt the blocking call —
`GeneratorExit` is only delivered when the generator is resumed. The result:

- The queue stays live for up to `_SSE_POLL_TIMEOUT_SEC` (15 s) after client disconnect.
- During this window, `subscriber_count()` is inflated by 1.
- If a new client reconnects before the old slot is released, there are transiently 2
  subscriptions for what is logically 1 client.
- This is harmless in practice (localhost, single Swift agent) but can confuse
  monitoring and makes `subscriber_count()` unreliable as a liveness indicator.

The WS path (`_handle_ws_connection`) does not have this race because `flask-sock`
drives the generator directly and delivers `GeneratorExit` on socket close.

**Recommended fix:** Document the 15 s teardown window in `sse_stream()` docstring.
Optionally, lower `_SSE_POLL_TIMEOUT_SEC` from 15 s to 5 s to reduce the window
without any other changes (keepalive frequency would increase from 15 → 5 s, which is
still valid). This is a documentation + constant change only.

---

### FINDING-4 — `emit()` drops-on-full but no eviction strategy for `realtime.partial_transcript` burst at 30 Hz

**Severity: LOW** (extends W800 ISSUE-2 / W892 §2.3)
**Files:** `KrabEar/backend/event_bus.py:62–81`, `KrabEar/backend/service.py:170`

W892 §2.2 documented that `recording.audio_level` fires at ~30 Hz and fills the 64-slot
queue in ~2.1 seconds. That analysis holds. A second high-frequency path was not
separately called out: `realtime.partial_transcript` fires at `rt_partial_interval_sec`
(default 3 s, configurable), but during a long recording session with a suspended SSE
subscriber:

- 64 partial events ÷ one event every 3 s = 192 s (~3.2 minutes) to fill.
- After that: every partial event logs a WARNING (`event_bus.py:81`).
- The WARNING line is unstructured (no `extra=` fields), so log-based alerting cannot
  filter it by event type.

For `recording.audio_level` at 30 Hz, the drop rate is 28.9 events/sec after the queue
fills — each triggers the `logger.warning()` call inside the `if dropped:` block (not
inside the loop, so only 1 warning per `emit()` call, i.e., 1 warning per audio chunk,
not 30/sec). Still, a 3–10 minute recording generates 180–600 WARNING lines about this
one non-critical high-frequency event.

**W892 recommendation still unimplemented:** Add `extra={"event_type": event_type, "dropped": dropped}`
to the `logger.warning()` call at `event_bus.py:81`. This costs 0 runtime overhead and
enables future `event_type` filtering in Sentry/Datadog without code changes.

---

### FINDING-5 — No test for `privacy_mode_enabled` blocking `realtime_partial` SSE emission

**Severity: INFO** (test coverage gap complementing FINDING-1)
**Files:** `KrabEar/tests/test_event_bus.py`, `KrabEar/tests/test_event_bus_extras.py`

The two event bus test files (`test_event_bus.py` — 7 test classes; `test_event_bus_extras.py` — 6 test classes) cover:
- subscribe/unsubscribe, full-queue isolation, SSE format, keepalive, filter, sentinel,
  concurrent subscribe/emit, typed emit validation, unicode.

No test verifies that `privacy_mode_enabled=True` suppresses partial transcript events
on the bus. There is also no test asserting the current behavior (that privacy mode does
NOT suppress them), which means FINDING-1 is a silent gap — the absence of a test means
it could be introduced as a regression without CI catching it.

**Recommended fix:** Add one test in `test_recording_core_service.py` (or a new
`test_event_bus_privacy.py`):
```python
def test_privacy_mode_suppresses_realtime_partial(self):
    svc = make_recording_core_svc(settings={"privacy_mode_enabled": True})
    bus = EventBus()
    q = bus.subscribe()
    svc.handle_start_recording({})
    # assert no "realtime.partial_transcript" event arrives on q
    ...
```

---

## 3. Additional Observations (Not New Findings)

### Eviction strategy — confirmed still correct

W892 §2.3 evaluated four options (LIFO, per-type sizing, suppress-near-full, drop-on-full)
and concluded drop-on-full (Option D) is correct for localhost single-process use.
Re-confirmed: no change needed.

### Heartbeat asymmetry (SSE 15 s vs WS 30 s) — still present

W892 §1.6 noted the asymmetry. Still not aligned. Acceptable, not a regression.

### SSE `retry:` field — still absent

W892 §1.3 recommended adding `yield "retry: 5000\n\n"` at stream start. Still not done.
Cost: 1 line. Still valid low-effort improvement.

### `subscriber_count()` production callers: 0

W800 ISSUE-3 — `subscriber_count()` is never called in production code. Still true.
The method exists only for testing. Not a bug.

### W800 ISSUE-1 (no `EventBus.close()`) — no change

`GracefulShutdownHandler` (`shutdown_handler.py`) still has no interaction with
`EventBus`. `sse_stream` subscribers are orphaned until the process exits. Acceptable
for the current use case.

---

## 4. Summary Table

| # | Finding | Severity | W892/W800 ref | New in W1194 |
|---|---------|----------|---------------|--------------|
| F-1 | `privacy_mode_enabled` does not gate `realtime.partial_transcript` SSE | MEDIUM | New | Yes |
| F-2 | `krab_error` + `realtime.final_transcript` still untyped — Swift decoder silent-failure risk | LOW | W892 P1a open | Yes (concrete risk articulation) |
| F-3 | SSE subscriber leak window: up to 15 s after client disconnect | LOW | W892-FINDING-5 extends | Yes (new mechanism) |
| F-4 | Drop warning unstructured — 600+ unfiltered WARNING lines on long recording | LOW | W892 §2.5 open | Yes (new quantification) |
| F-5 | No test coverage for privacy_mode + realtime_partial interaction | INFO | New | Yes |
| — | W892-FINDING-1 through FINDING-5 all open | varies | W892 | carried |
| — | W892 `retry:` SSE field still absent | LOW | W892 open | carried |
| — | W892 drop warning structured logging still not done | INFO | W892 open | carried |
| — | W892 typed emit migration P1a/P1b/P1c all open | — | W892 open | carried |

**5 new findings this pass.** All W892 findings remain open (0 addressed).

---

## 5. Cross-references

- **W800** (`2026-05-26-wave800-event-bus.md`) — original audit (6 issues, all open)
- **W892** (`2026-05-26-wave892-event-bus-deep.md`) — deep audit (5 findings + migration plan, all open)
- `KrabEar/backend/event_bus.py` — 131 lines
- `KrabEar/backend/rest_server.py:967–1088` — SSE + WS endpoints
- `KrabEar/backend/recording_core_service.py:137–196` — `handle_start_recording`, realtime_partial start
- `KrabEar/backend/realtime_partial.py:168–179` — partial transcript emit (no privacy gate)
- `KrabEar/backend/error_bus.py:189` — `krab_error` untyped emit
- `KrabEar/contracts/registry.py` — `EventType` enum (9 members; `KRAB_ERROR`, `REALTIME_PARTIAL`, `REALTIME_FINAL` all absent)
- `KrabEar/tests/test_event_bus.py`, `KrabEar/tests/test_event_bus_extras.py` — existing test coverage (no privacy tests)
