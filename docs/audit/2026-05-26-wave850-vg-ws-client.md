# Wave 850 — VGWebSocketClient Audit

**File:** `KrabEar/backend/vg_ws_client.py`
**Date:** 2026-05-26
**Lines:** 107
**Auditor:** wave850 sub-agent

---

## Summary

`VGWebSocketClient` is a 107-line async WebSocket client that subscribes to a Voice Gateway
session stream (`/v1/sessions/{id}/stream`) and forwards every received event into the in-process
`EventBus`. The code is lean and intentional. Six findings were identified: two medium, three low,
one info.

---

## Findings

### [MEDIUM] M1 — `stop()` does not interrupt an in-progress `asyncio.sleep` backoff

**Location:** `vg_ws_client.py:68–69`, `vg_ws_client.py:73–75`

`stop()` sets `self._stop`, but `await asyncio.sleep(backoff)` (up to 10 s) runs between
connection attempts. The `_stop` flag is only checked at the top of `while not self._stop.is_set()`
— after the sleep completes. If `stop()` is called during a reconnect cooldown the coroutine stays
alive for up to `_RECONNECT_MAX_SEC` (10 s) before actually exiting.

**Recommended fix:**

```python
try:
    await asyncio.wait_for(asyncio.shield(self._stop.wait()), timeout=backoff)
    break  # stop was signalled during sleep
except asyncio.TimeoutError:
    pass  # normal: sleep expired, try reconnect
```

Or simply:

```python
await asyncio.wait_for(self._stop.wait(), timeout=backoff)
break
```

This reduces shutdown latency from up to 10 s to near-instant.

---

### [MEDIUM] M2 — SSRF partial bypass via `http://localhost` path traversal variants

**Location:** `KrabEar/backend/settings_validator.py:208–214` (enforcement point for `gateway_url` passed to constructor)

The validator guards `voice_gateway_url` with a prefix check:

```python
gw_url.startswith("http://localhost")
or gw_url.startswith("http://127.0.0.1")
```

This blocks obvious external URLs but does **not** cover:

| Bypass vector | Example |
|---|---|
| IPv6 loopback | `http://[::1]:8090/...` |
| IPv6 any-bind | `http://[::]:8090/...` |
| `0.0.0.0` | `http://0.0.0.0:8090/...` |
| AWS metadata via redirect | attacker-controlled HTTPS endpoint redirects to `http://169.254.169.254/` |
| Link-local | `http://169.254.x.x/...` |

Practically the guard is already unusually tight for a local-only tool. The HTTPS branch (`startswith("https://")`) is deliberately open (tunnel scenarios) and cannot be fully locked down without breaking legitimate use. The primary gap is the missing IPv6 loopback check.

**Recommended fix (settings_validator.py):**

```python
_ALLOWED_GATEWAY_PREFIXES = (
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "https://",
)
if not gw_url.startswith(_ALLOWED_GATEWAY_PREFIXES):
    ...
```

---

### [LOW] L1 — Backoff resets to base on every successful connect even for 0-message sessions

**Location:** `vg_ws_client.py:43`

```python
backoff = _RECONNECT_BASE_SEC  # reset on connect
```

If the gateway accepts the connection but immediately closes it (empty session, server-side
timeout), the client resets backoff to 1 s each time and reconnects at full speed — effectively
a tight reconnect loop. This is not dangerous but can produce log spam and unnecessary
gateway load.

**Recommended fix:** Reset backoff only after the connection has produced at least one successful
message, not just on entering `async with`:

```python
connected_once = False
async for raw in ws:
    connected_once = True
    ...
if connected_once:
    backoff = _RECONNECT_BASE_SEC
```

---

### [LOW] L2 — `stop()` is not async-safe when called from a different event loop thread

**Location:** `vg_ws_client.py:73–75`

`self._stop` is an `asyncio.Event()` created in `__init__`, bound to whichever event loop is
running at construction time. `stop()` calls `self._stop.set()` directly. In Python 3.10+ this is
safe for cross-thread calls on most platforms. However, if the `VGWebSocketClient` is constructed
in one thread and `stop()` is called from a different OS thread that runs a different event loop
(or no loop), the behaviour is undefined before Python 3.10.

The project targets macOS 13+ and uses Python from `.venv_krab_ear`. If the Python version is
3.10+, this is benign. If < 3.10, a thread-safe wrapper is needed:

```python
def stop(self) -> None:
    loop = self._stop.get_loop()  # py3.10+
    loop.call_soon_threadsafe(self._stop.set)
```

**Action:** Verify Python version floor; document if 3.10 is assumed; add `loop.call_soon_threadsafe` for safety if not.

---

### [LOW] L3 — Contract validation warning swallows the bad event; EventBus still receives it

**Location:** `vg_ws_client.py:51–57`

```python
schema_cls = EVENT_SCHEMA_MAP.get(event_type)
if schema_cls:
    try:
        schema_cls.model_validate(event_data)
    except Exception as e:
        logger.warning("VG event %s failed contract validation: %s", event_type, e)
bus.emit(event_type, event_data)   # <-- fires regardless of validation outcome
```

An event that fails schema validation is still forwarded to all EventBus subscribers. Downstream
SSE consumers (Swift agent) receive malformed payloads they may not handle gracefully.

**Recommended fix:** Either skip the `bus.emit` on validation failure, or convert the warning
to a `vgw.contract_violation` error code and emit to ErrorBus instead of forwarding the raw
payload:

```python
if schema_cls:
    try:
        schema_cls.model_validate(event_data)
    except Exception as e:
        logger.warning(...)
        self._push_error("vgw.contract_violation", ...)
        continue   # do not forward invalid event
bus.emit(event_type, event_data)
```

---

### [INFO] I1 — No send path exists; client is purely read-only

**Location:** entire file

`VGWebSocketClient` provides no way to send messages back to the Voice Gateway over the
established WebSocket. Any upstream communication is done via REST calls in
`CallAssistService._assist_loop` (HTTP POST to `/v1/sessions/{id}/events`). This is consistent
with the current architecture (subscription-only SSE mirror over WS), but is non-obvious:
callers cannot use this client to push audio or control messages to the gateway.

This is a design note, not a bug. Worth documenting in docstring or a short comment.

---

## Coverage assessment

Existing test file (`KrabEar/tests/test_vg_ws_client.py`) covers:

- URL construction (http → ws, https → wss)
- `stop()` flag propagation
- Event forwarding to EventBus
- Reconnect on `ConnectionError` (2 connect calls, 1 sleep)
- Max backoff cap
- Event forwarding after reconnect
- Concurrent burst messages
- Clean shutdown (stop called before first message)
- Empty session_id handling
- `_push_error` Sentry guard (via `test_push_error_sentry_guard.py`)

Not covered by existing tests:

- M1: `stop()` during `asyncio.sleep` backoff (delayed shutdown)
- L1: tight reconnect loop on zero-message connects
- L3: EventBus receives contract-invalid event (no test asserts this is blocked)

---

## Verdict

The client is well-structured and the critical SSRF concern (gateway URL) is properly guarded
at the `settings_validator.py` layer. The main actionable items are M1 (shutdown latency during
reconnect backoff) and L3 (invalid events forwarded to EventBus). M2 is hardening-only (IPv6
loopback gap). No data loss or crash risk was found.

| ID | Severity | Title | Action |
|----|----------|-------|--------|
| M1 | Medium | `stop()` delayed up to 10 s during backoff sleep | Use `asyncio.wait_for(stop.wait(), timeout=backoff)` |
| M2 | Medium | SSRF: IPv6 loopback not blocked in validator | Add `http://[::1]` to allowed prefixes |
| L1 | Low | Backoff reset on zero-message connect → log spam | Reset only after first message |
| L2 | Low | `asyncio.Event.set()` cross-thread safety pre-3.10 | Use `call_soon_threadsafe` or document floor |
| L3 | Low | Contract-invalid events still forwarded to EventBus | `continue` on validation failure |
| I1 | Info | Client is read-only; no send path | Document in docstring |
