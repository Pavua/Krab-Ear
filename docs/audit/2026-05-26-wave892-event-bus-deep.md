# Wave 892 — EventBus Deep Audit: SSE/WS Specifics

**Date:** 2026-05-26
**Scope:** `KrabEar/backend/event_bus.py`, `KrabEar/backend/rest_server.py` (SSE + WS),
           `KrabEar/contracts/registry.py`, all emit sites
**Predecessor:** W800 (`2026-05-26-wave800-event-bus.md`) — surface-level audit. This
               doc goes deeper on three topics W800 deferred: SSE keep-alive mechanics,
               queue eviction strategy, and typed/untyped emit migration plan.
**Analyst:** Wave 892 audit pass

---

## Scope delta vs W800

W800 identified 6 issues and made general recommendations. This audit adds:

1. **SSE keep-alive** — proxy timeout analysis, `retry:` field gap, `id:` field gap
2. **Queue eviction strategy** — `_QUEUE_MAXSIZE=64` sizing rationale, eviction-vs-drop
   design options, high-frequency emitter math
3. **Typed/untyped migration plan** — concrete wave-by-wave migration order for the 17
   untyped sites, effort estimates, risk classification

W800 findings that remain open are referenced but not re-documented here.

---

## 1. SSE Keep-Alive Deep Analysis

### 1.1 Current mechanism

```python
# event_bus.py
_SSE_POLL_TIMEOUT_SEC = 15.0

def sse_stream(bus, event_filter=None):
    q = bus.subscribe()
    try:
        while True:
            try:
                event = q.get(timeout=_SSE_POLL_TIMEOUT_SEC)
            except queue.Empty:
                yield ": keepalive\n\n"   # SSE comment — invisible to clients
                continue
            ...
            yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
    finally:
        bus.unsubscribe(q)
```

The keep-alive is a plain SSE comment (`: keepalive\n\n`). This is valid per the SSE
spec (RFC 8895 §9.1). Comments keep the TCP connection alive and prevent
proxy/load-balancer idle timeouts.

### 1.2 Keep-alive interval vs common proxy timeouts

| Proxy / default | Idle timeout | Status vs 15 s keepalive |
|-----------------|-------------|--------------------------|
| nginx `proxy_read_timeout` | 60 s | Safe — keepalive fires every 15 s |
| HAProxy `timeout tunnel` | 1 h (default) | Safe |
| AWS ALB idle timeout | 60 s (default) | Safe |
| Cloudflare proxy | 100 s | Safe |
| macOS `launchd` (localhost) | N/A | No proxy in production path |

**Finding:** The 15 s interval is conservative and correct for all common proxy
configurations. No change needed.

### 1.3 MISSING: `retry:` field in SSE stream

**Severity: LOW**

The SSE spec allows servers to set the browser's automatic reconnect delay via:

```
retry: 3000\n\n
```

The current `sse_stream` never emits a `retry:` field. Browser SSE clients that lose
the connection (backend restart, network blip) will use the browser's default reconnect
interval — typically 3 s in Chrome/Firefox, which is acceptable. However:

- After a planned restart (`GracefulShutdownHandler`), there is no shutdown broadcast
  (W800 ISSUE-1), so clients reconnect immediately after the default delay. If the
  backend takes >3 s to restart, the client will attempt a reconnect during startup,
  receive a connection-refused error, and retry again.
- There is no way for the server to signal a longer delay (e.g., 30 s during a heavy
  STT model reload).

**Recommended fix (LOW effort):** Emit a `retry: 5000\n\n` frame once at stream start,
immediately after subscribe. This sets 5 s browser reconnect backoff — matching the
backend's typical restart time — with no further changes required.

```python
def sse_stream(bus, event_filter=None):
    q = bus.subscribe()
    yield "retry: 5000\n\n"          # <-- add this line
    try:
        ...
```

### 1.4 MISSING: `id:` field — no Last-Event-ID support

**Severity: INFO**

SSE supports resumable streams via `id:` fields on each event. When the browser
reconnects it sends `Last-Event-ID: <last_id>` in the request header, allowing the
server to replay missed events.

The current implementation has no `id:` on events and no replay buffer. This is
intentional by design — `EventBus` is a real-time push bus without persistent history
(that's `StateStore`'s job). However:

- SSE clients (Swift `LiveSubtitlesOverlay.swift`, `main+Errors.swift`) reconnect after
  backend restart and miss all events between disconnect and reconnect (e.g., the
  `stt.final` that triggered the restart).
- The `event_replay.py` module (`EventReplayManager`) exists but is not wired to the
  SSE endpoint.

**Assessment:** Acceptable gap for a localhost assistant. The `stt.final` event is
already persisted to `StateStore`, so the Swift agent can query history after reconnect.
Document as known limitation; no fix required unless external SSE consumers are added.

### 1.5 SSE CORS gap

**Severity: INFO**

The `/v1/events` endpoint does not set `Access-Control-Allow-Origin`. The Flask-SMOREST
`api` object may set CORS headers if configured, but no explicit CORS headers appear in
the `events_stream()` response or in the global app configuration in `rest_server.py`.

For the current use case (macOS native app + localhost), this is not a problem — CORS
applies only to cross-origin browser requests. Flag if a web-based dashboard consumer
is ever added.

### 1.6 WS heartbeat asymmetry vs SSE keepalive

| Transport | Keep-alive interval | Mechanism |
|-----------|-------------------|-----------|
| SSE (`/v1/events`) | 15 s | SSE comment `": keepalive\n\n"` |
| WS (`/ws/events`) | 30 s | JSON `{"type":"ping"}` frame |

The WS heartbeat interval (30 s) is double the SSE interval (15 s). This is inconsistent
but not harmful — WS connections are full-duplex and the OS TCP keepalive also applies.
Consider aligning both to 15 s for consistency in a future cleanup pass.

---

## 2. Queue Eviction Strategy Analysis

### 2.1 Current design: drop-on-full, no eviction

```python
_QUEUE_MAXSIZE = 64

def emit(self, event_type, payload):
    ...
    for q in active:
        try:
            q.put_nowait(event)
        except queue.Full:
            dropped += 1
    if dropped:
        logger.warning("EventBus: %d подписчик(ов) пропустили событие %s", dropped, event_type)
```

When a subscriber queue is full, the newest event is dropped and the oldest events are
preserved. This is FIFO-queue semantics: new arrivals are rejected when at capacity.

### 2.2 High-frequency emitter math

`recording.audio_level` is emitted from the audio callback at ~30 Hz (16 kHz PCM,
512-frame chunks → 31.25 events/sec).

| Queue size | Time-to-fill at 30 Hz | Time-to-fill at 1 Hz (other events) |
|------------|----------------------|--------------------------------------|
| 64 (current) | **~2.1 seconds** | ~64 seconds |

A suspended browser tab or slow SSE consumer fills the queue in ~2 s during recording,
then drops every subsequent audio_level event. The WARNING log fires for every dropped
event — potentially 31 lines/sec — as noted in W800 ISSUE-2.

### 2.3 Eviction alternatives considered

**Option A: LIFO (newest-event-wins) — replace oldest on full**

```python
# Hypothetical: evict-oldest instead of drop-newest
try:
    q.put_nowait(event)
except queue.Full:
    try:
        q.get_nowait()   # discard oldest
    except queue.Empty:
        pass
    q.put_nowait(event)  # put newest
```

Pros: newest state always available to slow consumer (better for VU meter / status).
Cons: races possible in multi-threaded context (get_nowait → put_nowait not atomic).
      Requires a lock or a custom queue class. Complexity cost high.

**Option B: Per-event-type size differentiation**

Give high-frequency event types a smaller cap (e.g., `recording.audio_level` = 4,
`stt.final` = 64). Requires replacing the single `queue.Queue` with a type-aware filter
layer — significant refactor.

**Option C: Suppress high-frequency events when queue is near-full**

In `emit()`, before `put_nowait`, check if the event type is in a `_HF_EVENT_TYPES`
set and if `q.qsize() > THRESHOLD` skip without even attempting the put. Avoids the
`queue.Full` exception overhead for audio_level.

**Option D: Current design with log debounce only (W800 recommendation)**

Keep drop-on-full as-is, fix only the WARNING log spam. Simplest, correct for a
single-process localhost assistant.

**Assessment:** Option D is correct for the current use case. Options A–C add
complexity that is not justified until multi-client or networked scenarios arise. The
architectural simplicity of the current design (no state, no ordering guarantees) is a
feature.

### 2.4 `_QUEUE_MAXSIZE = 64` sizing assessment

For non-audio-level events (stt.final, krab_error, app.status):

- The busiest scenario is a batch import: `recording_core_service.py` emits one
  `STT_FINAL` per file. A 64-file batch at ~1 event/file fills the queue exactly once.
  In practice imports take 3–30 s each, so the consumer has ample time to drain.
- `krab_error` events: error_bus has its own ring buffer (dedupe); bursts are rare.

**Assessment:** 64 is appropriate. No change needed.

### 2.5 Missing metric: per-subscriber drop counter

W800 ISSUE-3 notes that `subscriber_count()` is never surfaced in production. An
additional gap: there is no per-subscriber or per-event-type drop counter. The WARNING
log line is the only signal, and it is not structured (no `extra={}` fields).

**Finding:** The WARNING log line at `event_bus.py:81` should use structured logging:

```python
logger.warning(
    "EventBus: %d подписчик(ов) пропустили событие %s (очередь полна)",
    dropped, event_type,
    extra={"event_type": event_type, "dropped": dropped},
)
```

This enables future log-based alerting (Sentry `event_bus.drop` breadcrumb) without
code changes.

---

## 3. Typed vs Untyped Emit Migration Plan

### 3.1 Current state

| Category | Count |
|----------|-------|
| Typed (`emit_typed`) call sites | 6 |
| Untyped (`emit`) call sites | 17 (16 real + 1 dynamic passthrough) |
| EventType enum members | 9 |
| Untyped strings with no EventType | 8 consumer-facing + 4 infra |

### 3.2 Consumer-facing untyped events (Priority 1 — migrate first)

These are received by Swift SSE consumers (`main+Errors.swift`, `LiveSubtitlesOverlay`,
`main+LiveSubs.swift`) or external REST clients. Schema drift here causes silent parse
failures.

| String key | File | Complexity | Risk if untyped | Suggested EventType |
|-----------|------|------------|-----------------|---------------------|
| `"realtime.partial_transcript"` | `realtime_partial.py:169` | Low | Medium — Swift overlay parses it | `REALTIME_PARTIAL` |
| `"realtime.final_transcript"` | `recording_core_service.py:1241` | Low | Medium — call-assist session uses it | `REALTIME_FINAL` |
| `"krab_error"` | `error_bus.py:189` | Low — `KrabError` Pydantic already exists | High — Swift `ErrorActionHandler.swift` parses `KrabErrorPayload` | `KRAB_ERROR` |
| `"app.status"` | `recording_core_service.py:368`, `obsidian_sync.py` (4 sites) | Medium — 5 distinct payload shapes | Low — not parsed by Swift (monitoring only) | `APP_STATUS` |

**Wave plan for Priority 1:**

1. `KRAB_ERROR` — easiest: `KrabError` Pydantic model already exists in `error_bus.py`.
   Add `EventType.KRAB_ERROR = "krab_error"` to registry, add `EVENT_SCHEMA_MAP` entry,
   change `error_bus.py:189` to `self._event_bus.emit_typed(EventType.KRAB_ERROR, payload)`.
   **Effort: ~15 min. Risk: zero (model already correct).**

2. `REALTIME_PARTIAL` / `REALTIME_FINAL` — add models in new `contracts/realtime_events.py`:
   ```python
   class RealtimePartial(BaseModel):
       text: str
       duration_sec: float | None = None
       session_id: str | None = None

   class RealtimeFinal(BaseModel):
       session_id: str
       text: str
       is_partial: bool = False
       ts: float
   ```
   **Effort: ~30 min. Risk: low — verify `realtime_partial.py` emit dict matches model.**

3. `APP_STATUS` — define `AppStatusEvent(BaseModel)` with all optional fields:
   ```python
   class AppStatusEvent(BaseModel):
       status: str | None = None
       message: str | None = None
       recording_id: str | None = None
       sync_count: int | None = None
       file: str | None = None
       error: str | None = None
   ```
   Migrate all 5 call sites. **Effort: ~45 min. Risk: medium — 5 sites to update.**

### 3.3 Operational untyped events (Priority 2 — migrate after P1)

Consumed by monitoring/logging but not by Swift clients.

| String key | File | Suggested EventType |
|-----------|------|---------------------|
| `"rewriter_recovered"` | `llm_probe.py:253` | `REWRITER_RECOVERED` |
| `"preset.changed"` | `settings_service.py:360` | `PRESET_CHANGED` |
| `"bulk_reprocess_progress"` | `bulk_reprocess.py:119` | `BULK_REPROCESS_PROGRESS` |
| `"playback.seek"` | `bookmarks.py:266` | `PLAYBACK_SEEK` |
| `"recording.silence_detected"` | `realtime_silence_filter.py:158` | `RECORDING_SILENCE_DETECTED` |
| `"recording.audio_level"` | `service.py:181` | `RECORDING_AUDIO_LEVEL` |

**Note on `recording.audio_level`:** This is high-frequency (~30 Hz). Creating a typed
model adds Pydantic serialization cost per event. Given W800 ISSUE-2 analysis, this is
the one site that should remain untyped OR have its model kept to a single float field
to minimize serialization overhead.

### 3.4 Infra events (Priority 3 — defer)

| String key | Notes |
|-----------|-------|
| `f"disk.{level}"` (dynamic) | Dynamic key; `DISK_WARNING` + `DISK_CRITICAL` would require two separate types or a severity field |
| `"disk.history_large"` | Low-frequency, internal |
| `"disk.auto_cleanup_requested"` | Low-frequency, internal |

Defer until disk monitoring is refactored. The dynamic `f"disk.{level}"` pattern is
the blocker — typed emit cannot use dynamic EventType keys without an adapter.

**Workaround:** Change `disk_monitor.py:228` to explicit branching:
```python
if level == "warning":
    self._event_bus.emit_typed(EventType.DISK_WARNING, DiskEvent(...))
elif level == "critical":
    self._event_bus.emit_typed(EventType.DISK_CRITICAL, DiskEvent(...))
```

### 3.5 Dynamic / intentionally untyped (leave as-is)

| Pattern | File | Reason |
|---------|------|--------|
| `bus.emit(event_type, event_data)` | `vg_ws_client.py:57` | Forwards VG WS wire frames verbatim; event_type comes from external protocol |

This site correctly stays untyped. Adding a typed wrapper would require maintaining a
mapping of all Voice Gateway event types — a separate concern.

### 3.6 Migration order summary

| Wave | Sites | EventType additions | Effort |
|------|-------|---------------------|--------|
| W893-P1a | `error_bus.py:189` | `KRAB_ERROR` | ~15 min |
| W893-P1b | `realtime_partial.py:169`, `recording_core_service.py:1241` | `REALTIME_PARTIAL`, `REALTIME_FINAL` | ~30 min |
| W893-P1c | `recording_core_service.py:368` + 4× `obsidian_sync.py` | `APP_STATUS` | ~45 min |
| W894 | `llm_probe.py`, `settings_service.py`, `bulk_reprocess.py`, `bookmarks.py`, `realtime_silence_filter.py` | 5 new types | ~60 min |
| W895 | `disk_monitor.py` (3 sites) | `DISK_WARNING`, `DISK_CRITICAL`, `DISK_HISTORY_LARGE`, `DISK_AUTO_CLEANUP_REQUESTED` | ~45 min |
| defer | `service.py:181` (`recording.audio_level`) | `RECORDING_AUDIO_LEVEL` | after perf benchmark |
| never | `vg_ws_client.py:57` | none | intentional |

Total new EventType members after full migration: 9 existing + 12 new = **21 members**.

---

## 4. Additional Findings (SSE/WS Specifics)

### FINDING-1 — SSE endpoint docstring lists only 2 event types

**Severity: INFO**
**File:** `rest_server.py:1015–1019`

The `events_stream()` docstring documents only `stt.final` and `stt.failed` but the
endpoint delivers all 17+ event types. Consumers must guess what else arrives.

**Fix:** Update docstring to list all documented event types, or reference
`contracts/registry.py` and the known untyped types.

### FINDING-2 — WS `_handle_ws_connection` receives `bus` param but SSE does not

**Severity: INFO**
**Files:** `rest_server.py:1046`, `rest_server.py:1023`

`_handle_ws_connection(ws, bus, ...)` receives the bus as a parameter (enabling unit
testing with a mock bus). The SSE endpoint `events_stream()` uses the module-level
`event_bus` singleton directly:

```python
stream_with_context(sse_stream(event_bus, event_filter=event_filter))
```

This asymmetry makes the SSE path harder to unit-test in isolation. Align by extracting
an `_sse_response(bus, event_filter)` helper that accepts `bus` as a parameter.

### FINDING-3 — No SSE connection count limit

**Severity: LOW**
**File:** `rest_server.py:996–1029`

The `/v1/events` endpoint has no per-IP or global connection limit. A misconfigured
Swift client reconnecting in a tight loop (e.g., due to W800 ISSUE-1 — no shutdown
sentinel) would create unbounded subscribers, each holding a 64-event queue. At 100
connections that is 100 × 64 × ~500 bytes = ~3 MB of queued event data, plus 100 live
Python generator frames.

In practice the Swift agent opens exactly one SSE connection. However, if the agent
reconnects before the old connection is garbage-collected (Flask does not cancel the
old generator synchronously), subscriber_count transiently exceeds 1.

**Fix:** Add `if bus.subscriber_count() >= MAX_SSE_CONNECTIONS: return 503` guard with
`MAX_SSE_CONNECTIONS = 10` (generous local limit). This is the same pattern WS servers
use for connection limits.

### FINDING-4 — WS auth bypass path: `REST_API_KEY` empty string

**Severity: LOW**
**File:** `rest_server.py:1123–1141`

In `_ws_check_auth()`, when `REST_API_AUTH_ENABLED=False` and `REST_API_KEY` is set:

```python
api_key = settings.REST_API_KEY
if api_key:                       # ← falsy check
    raw = _raw_token()
    ok = hmac.compare_digest(...)
```

If `REST_API_KEY` is set to an empty string `""`, `if api_key:` is False and auth is
skipped. This mirrors the SSE `require_api_key` decorator behavior and is consistent,
but the docstring implies any non-None key activates auth. Worth documenting explicitly.

### FINDING-5 — `sse_stream` generator leak on client abort (Flask dev server only)

**Severity: INFO (production not affected)**
**File:** `event_bus.py:92–126`, `rest_server.py:1022`

Under `flask-sock` (WS) the `finally: bus.unsubscribe(q)` is guaranteed because the
generator is driven by the sock library. Under Flask's `stream_with_context` for SSE,
if the client disconnects abruptly (TCP RST), Flask's Werkzeug dev server may not
propagate the `GeneratorExit` to `sse_stream`, leaving the subscriber alive until the
next keepalive timeout (15 s) causes a write attempt to the dead socket, which then
raises an exception and triggers the finally block.

In production (gunicorn with gevent or eventlet), behavior depends on the WSGI worker
type. Gunicorn sync workers will block on the next `yield` until the OS delivers a
broken-pipe error. The `finally: bus.unsubscribe(q)` will then execute correctly.

This is a known Flask SSE limitation. No action required, but document in `sse_stream`
docstring that subscriber cleanup on abrupt disconnect depends on WSGI worker behavior.

---

## 5. Summary Table

| Finding | Severity | Effort | W800 ref |
|---------|----------|--------|----------|
| FINDING-1: SSE docstring incomplete event type list | INFO | Trivial | New |
| FINDING-2: SSE endpoint not injectable (no `bus` param) | INFO | Small | New |
| FINDING-3: No SSE connection count limit | LOW | Small | New |
| FINDING-4: WS auth bypass on empty-string API key | LOW | Trivial doc | New |
| FINDING-5: Generator leak on client abort (dev server) | INFO | Doc only | New |
| MISSING `retry:` field in SSE stream | LOW | Trivial (1 line) | New |
| MISSING `id:` / Last-Event-ID support | INFO | Out of scope | New |
| Drop warning not structured | INFO | Trivial | Extends W800-ISSUE-2 |
| Migration plan P1a: `krab_error` → typed | — | 15 min | New |
| Migration plan P1b: `realtime.*` → typed | — | 30 min | New |
| Migration plan P1c: `app.status` → typed | — | 45 min | New |

---

## 6. Cross-references

- **W800** (`2026-05-26-wave800-event-bus.md`) — original EventBus audit (6 open issues)
- **W787** (`2026-05-26-wave787-contracts-audit.md`) — untyped emit site inventory
- **W809** (`2026-05-26-wave809-rest-server-audit.md`) — REST server audit (WS auth)
- `KrabEar/backend/event_bus.py` — implementation (131 lines)
- `KrabEar/backend/rest_server.py:996–1088` — SSE + WS endpoints
- `KrabEar/contracts/registry.py` — EventType enum + EVENT_SCHEMA_MAP
- `KrabEar/backend/error_bus.py` — KrabError model (P1a migration candidate)
- `KrabEar/backend/shutdown_handler.py` — does not touch EventBus (W800 ISSUE-1)
