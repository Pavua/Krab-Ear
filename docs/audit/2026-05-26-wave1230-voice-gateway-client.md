# Audit: VoiceGatewayClient REST — W1230

**Date:** 2026-05-26  
**Branch:** audit/voice-gateway-client-W1230  
**Scope:** `KrabEar/backend/call_assist_service.py` — `VoiceGatewayClient` class (lines 23–163) and its callers in `CallAssistService`.

---

## Location

`VoiceGatewayClient` is inlined in `KrabEar/backend/call_assist_service.py` (not a standalone module).  
It is a static-method HTTP client (using Python stdlib `urllib.request`) for the Voice Gateway REST API.  
`VGWebSocketClient` (`backend/vg_ws_client.py`) is a separate, async WebSocket client — not audited here.

---

## Findings

### F1 — No connection pooling: new TCP connection per call (MED)

`VoiceGatewayClient` uses `urllib.request.urlopen()` directly, creating a new TCP connection on every call. The `_assist_loop` background thread calls `gateway.post()` every ~1.5 s during an active call assist session, which means a fresh TCP handshake each iteration. The WS client (`VGWebSocketClient`) holds a persistent connection. Switching the REST client to `http.client.HTTPConnection` with keep-alive (or `urllib3`/`requests`) would halve round-trip overhead on loopback.

**File:** `call_assist_service.py:15` — `from urllib import ... request as urllib_request`

---

### F2 — No retry on transient network errors for stateful operations (MED)

`start_session` and `stop_session` have `SESSION_LIFECYCLE_TIMEOUT = 3.5 s` but no retry logic on `urllib_error.URLError` (e.g. `ConnectionRefused`, `timeout`). If the Gateway is momentarily unavailable at call start, `handle_start` records `gateway_status = "degraded"` and silently skips launching `_assist_loop` — the session proceeds without Gateway integration. At call stop, a failed `stop_session` is also silently swallowed (recorded only in `state["gateway_stop_error"]`), with no cleanup or retry.

The `_assist_loop` does implement exponential backoff (`_BACKOFF_STEPS = [0.5, 1.0, 2.0, 4.0]`) for continuous POST events — but only for that one path. Lifecycle transitions are fire-once.

**Files:** `call_assist_service.py:291–311` (start), `call_assist_service.py:424–434` (stop)

---

### F3 — `get()` accepts absolute URLs bypassing settings validation (LOW-MED)

The `get()` static method contains a special branch: if `path` already starts with `http://` or `https://`, it is used verbatim as the request URL, ignoring `voice_gateway_url` entirely (line 66–67). No caller currently passes absolute URLs, but the branch is dead code that could silently allow an unexpected URL to bypass `settings_validator.py`'s localhost/HTTPS guard (lines 209–216 in `settings_validator.py`). The `post()` and `delete()` methods do not have this branch; the inconsistency is a maintenance hazard.

**File:** `call_assist_service.py:66–67`

---

### F4 — `stop_session` error body not captured (LOW)

When `stop_session` receives an `urllib_error.HTTPError`, it discards the response body and returns only `{"ok": False, "error": "http_<code>"}` (line 160). All other methods (`start_session`, `get`, `post`, `delete`) read and include the response body in the error string (lines 51–53, 81–83, etc.). This makes `stop_session` failures opaque when the Gateway returns a 4xx/5xx with a JSON error payload — debugging requires resorting to Gateway logs rather than the Krab Ear breadcrumb.

**File:** `call_assist_service.py:159–161`

---

### F5 — Missing `Accept: application/json` header on GET/DELETE (LOW)

`start_session` and `post` add `Content-Type: application/json`, but none of the four methods add `Accept: application/json`. For a strictly-typed gateway this is a correctness hazard: if the server negotiates content type based on `Accept` and defaults to `text/plain`, `json.loads(raw)` will raise `json.JSONDecodeError` and be swallowed by the `except Exception` catch, returning `{"ok": False, "error": "..."}` silently.

**Files:** `call_assist_service.py:40–43`, `72–74`, `100–104`, `129–131`, `154–156`

---

### F6 — ErrorBus not used; _assist_loop exceptions logged only (LOW)

`_assist_loop` catches all exceptions with `logger.exception("Call Assist loop error")` (line 1090). The Phase B `ErrorBus` / `error_codes.py` is not wired here — no `_push_error` call. Persistent loop failures are silent to the user (no toast, no Sentry breadcrumb). By contrast, `handle_start` and `handle_stop` do call `add_breadcrumb()`, so the asymmetry is notable.

**File:** `call_assist_service.py:1089–1091`

---

## Summary

| # | Finding | Severity | File:Line |
|---|---------|----------|-----------|
| F1 | No connection pooling — new TCP per call in hot loop | MED | `call_assist_service.py:15,1062` |
| F2 | No retry on lifecycle calls (start/stop session) | MED | `call_assist_service.py:291,424` |
| F3 | `get()` absolute-URL bypass ignores settings validation | LOW-MED | `call_assist_service.py:66` |
| F4 | `stop_session` error body silently discarded | LOW | `call_assist_service.py:160` |
| F5 | Missing `Accept: application/json` header | LOW | `call_assist_service.py:40–156` |
| F6 | `_assist_loop` exceptions not wired to ErrorBus | LOW | `call_assist_service.py:1090` |

**Test coverage:** 5 test files, ~101 test methods (`test_call_assist_service*.py`, `test_call_assist_breadcrumbs.py`, `test_call_assist_quick_phrases_offline.py`). Core paths well covered via stub `VoiceGatewayClient` subclasses. Connection-level failures (pooling, TCP reset mid-call) are not covered.

**Settings re-read:** All `handle_*` methods call `self.store.load_settings()` fresh — compliant with post-W1167 pattern.

**WS client interaction:** `VGWebSocketClient` (`vg_ws_client.py`) is started separately from Swift side via IPC `call_assist_start`; it holds a persistent WS connection. `VoiceGatewayClient` REST and `VGWebSocketClient` are independent — no shared state or coordination issue observed.
