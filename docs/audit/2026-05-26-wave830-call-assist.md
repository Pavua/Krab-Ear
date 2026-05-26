# Wave 830 Audit: `backend/call_assist_service.py`

**Date:** 2026-05-26  
**File:** `KrabEar/backend/call_assist_service.py` (1149 LOC)  
**Scope:** Code quality, state machine correctness, VoiceGatewayClient HTTP lifecycle, `_assist_loop` thread lifecycle, error paths, security (URL injection, SSRF), dead handler surface overlap.

---

## Summary

The module is functionally sound and properly adheres to the service extraction pattern. The lock discipline on `_state` is correct: all reads and writes go through `self._lock`. Seven issues were found: one medium-severity state machine gap, two low-severity correctness issues, one medium-severity SSRF/injection risk in `VoiceGatewayClient.get`, two minor design gaps, and one housekeeping note on dead handlers.

| ID | Severity | Category | Finding |
|----|----------|----------|---------|
| F1 | MEDIUM | State machine | Double-start not guarded: second `handle_start` while active silently spawns second `_assist_loop` thread |
| F2 | LOW | Thread lifecycle | `handle_stop` never calls `recorder.stop()` — recording continues after call assist stops |
| F3 | MEDIUM | Security | `VoiceGatewayClient.get` accepts an absolute URL from `path`, enabling SSRF from a tampered settings value |
| F4 | LOW | URL injection | `source_lang`, `target_lang`, `category` params in `handle_list_quick_phrases` are interpolated into query string without `urllib_parse.quote` |
| F5 | LOW | Timezone | `started_at` / `stopped_at` are naive local-time ISO strings; the duration calc assigns `timezone.utc` to them, giving wrong duration for non-UTC systems |
| F6 | LOW | Error visibility | Gateway failures in `_assist_loop` only log a warning; no `error_bus` push is wired, unlike `VGWebSocketClient._push_error` |
| F7 | INFO | Dead handlers | Five `handle_*` methods in this file are wired to IPC methods that Wave 786 confirmed have zero callers (`call_assist_add_template`, `call_assist_list_templates`, `call_assist_remove_template`, `call_assist_template`, `call_assist_cost_report`) |

---

## F1 — Double-start spawns second `_assist_loop` (MEDIUM)

**Location:** `handle_start` lines 245–350, `_assist_loop` lines 1017–1092

**Description:**  
`handle_start` does not check whether a session is already active before proceeding. A second IPC call to `start_call_assist` while `self._state["active"] == True`:

1. Creates a new `session_id` and overwrites `self._state` with `active=True` and a fresh `gateway_session_id`.
2. Starts a new `_assist_loop` daemon thread passing the *new* `gateway_session_id`.
3. The original `_assist_loop` from the first call continues running. Its loop condition `self._state.get("active")` is still True (the new state also has `active=True`), so it never breaks. It continues posting to the *old* `gateway_session_id`.

Result: two daemon threads posting STT partials to two different gateway sessions simultaneously, until the `recorder` stops or the backend restarts. The previous gateway session is not explicitly stopped either.

**Existing test coverage:** `test_concurrent_start_blocked` (line 552 of `test_call_assist_service_deep.py`) acknowledges this behaviour: "A second start while already active — new session replaces old one." The test passes because it only checks state consistency, not thread count or whether the old gateway session was stopped.

**Recommendation:**  
Add an active-session guard at the top of `handle_start`:

```python
with self._lock:
    if self._state.get("active"):
        return dict(self._state)  # idempotent: return current state
```

Alternatively, if re-start is intentional, call `handle_stop({})` first to drain the old session cleanly before creating a new one.

---

## F2 — `handle_stop` does not stop the recorder (LOW)

**Location:** `handle_stop` lines 352–464

**Description:**  
`handle_start` starts recording if `recorder.is_recording` is False (line 247–251). However, `handle_stop` never calls `self.recorder.stop()`. After a call ends, the microphone continues running. The `_assist_loop` thread will observe `recorder.is_recording == True` and keep running until it separately polls `self._state["active"]` and exits on the next iteration (1–1.5 s delay).

This is partially mitigated because the `_assist_loop` checks `self._state.get("active")` at the top of every loop iteration, so it will terminate within 1.5 s. However, the recorder itself stays open. If no other code stops it, the microphone remains active after the call ends, which is unexpected from a user-privacy perspective.

**Recommendation:**  
Add `self.recorder.stop()` at the start of `handle_stop` (before or after the lock section), symmetrically to the `recorder.start()` call in `handle_start`. Guard with `if self.recorder.is_recording`.

---

## F3 — SSRF via absolute URL bypass in `VoiceGatewayClient.get` (MEDIUM)

**Location:** `VoiceGatewayClient.get` lines 64–86

**Code:**
```python
if path.startswith("http://") or path.startswith("https://"):
    url = path
else:
    url = f"{voice_gateway_url.rstrip('/')}{path}"
```

**Description:**  
The `path` parameter is passed directly by all callers inside this file. In all current call sites `path` is a static string literal like `f"/v1/sessions/{gw_sid}/timeline"` — the parameter value does not come from user IPC params.

However, `voice_gateway_url` **does** come from IPC-settable `settings`. If an attacker can write `voice_gateway_url` to point at an internal resource (e.g. `http://169.254.169.254/`), all gateway calls will SSRF to that target. The absolute-URL bypass branch would additionally let a malicious `path` (if it were ever caller-controlled) redirect to an arbitrary host while still sending the Bearer API key.

The `voice_gateway_url` field is intended to be the self-hosted gateway URL, so SSRF via settings is the primary concern. A URL allowlist or scheme+hostname validation in `_gateway_context` / `handle_start` would mitigate this.

**Recommendation:**  
1. Remove the `if path.startswith("http://")` absolute-URL bypass in `get()`. All callers use relative paths; there is no legitimate use case.
2. In `handle_start` and `_gateway_context`, validate that `voice_gateway_url` uses `http://` or `https://` and points to `localhost` / `127.0.0.1` / a configurable allowlist, before making any HTTP call.

---

## F4 — Missing URL encoding for lang/category query params (LOW)

**Location:** `handle_list_quick_phrases` lines 601–608

**Code:**
```python
query = (
    f"/v1/quick-phrases?source_lang={source_lang}"
    f"&target_lang={target_lang}&category={category}&limit={limit}"
)
```

**Description:**  
`source_lang`, `target_lang`, and `category` are lower-cased but not URL-encoded before interpolation. The other parameterised gateway call in the same file — `handle_cost_estimate` (line 718) and `handle_timeline` (lines 767–769) — correctly wraps user strings with `urllib_parse.quote(…, safe='')`. If a caller passes a `category` value containing `&` or `=` (e.g. `"all&admin=true"`), the query string is corrupted.

**Recommendation:**  
Wrap all three params with `urllib_parse.quote(source_lang, safe='')`, etc.

---

## F5 — Naive local datetime treated as UTC in duration calc (LOW)

**Location:** `handle_start` line 285, `handle_stop` lines 354 and 445–453

**Code:**
```python
started_at = datetime.now().isoformat(timespec="seconds")  # local naive
stopped_at = datetime.now().isoformat(timespec="seconds")  # local naive
# ...
if started_dt.tzinfo is None:
    started_dt = started_dt.replace(tzinfo=timezone.utc)  # wrong assumption
```

**Description:**  
`datetime.now()` returns a local-time naive datetime. `isoformat()` on a naive datetime produces a string without timezone offset. When the duration is computed in `handle_stop`, the code assigns `timezone.utc` to both timestamps if `tzinfo is None`. On machines where local time is not UTC (e.g. CEST = UTC+2), both timestamps are shifted by the same offset so the **delta** is still correct. However, the assigned UTC timezone is semantically wrong: the stored `started_at` will appear to be a UTC time even though it was local time. Any consumer that compares `started_at` against a true UTC timestamp will get an incorrect result.

**Recommendation:**  
Use `datetime.now(tz=timezone.utc).isoformat(timespec="seconds")` for both `started_at` and `stopped_at`. Remove the `replace(tzinfo=...)` guard in the duration calc — it will be unnecessary.

---

## F6 — Gateway failures in `_assist_loop` not pushed to error_bus (LOW)

**Location:** `_assist_loop` lines 1073–1086

**Description:**  
When a gateway POST fails inside `_assist_loop`, the code logs a warning and backs off. It does not push a `KrabError` to the error bus. By contrast, `VGWebSocketClient._push_error` (in `vg_ws_client.py`) does push a `vgw.reconnect` error on disconnect. The call-assist loop uses a different transport (HTTP polling instead of WebSocket), but the user-visible effect is the same: translations stop appearing.

The `error_codes.py` `vgw.reconnect` code at line 469–473 is the closest match; a call-assist-specific code (e.g. `vgw.call_assist_post_failed`) does not yet exist.

**Recommendation:**  
After `_BACKOFF_MAX` is reached (4 consecutive failures), push a `vgw.reconnect`-level error via `error_bus` (injected or imported) so the Swift toast and Sentry breadcrumb system is informed. This matches Phase B "Loud Errors" intent.

---

## F7 — Five handlers implemented here have zero IPC callers (INFO)

**Confirmed by Wave 786 dead-handler audit:**

| Method | IPC method name |
|--------|----------------|
| `handle_list_templates` | `call_assist_list_templates` |
| `handle_add_template` | `call_assist_add_template` |
| `handle_remove_template` | `call_assist_remove_template` |
| `handle_template` | `call_assist_template` |
| `handle_cost_report` | `call_assist_cost_report` |

These five methods (and their corresponding IPC registrations in `service.py` lines 1154–1158) were confirmed dead by the W786 three-scope audit. The template CRUD logic is self-contained and correct; the `handle_cost_report` method duplicates some logic from `handle_cost_estimate` (which IS actively used). These are candidates for removal in a future cleanup wave.

---

## Additional Notes

### Lock discipline — PASS

All reads and mutations of `self._state` are performed under `self._lock`. The `state` property returns a shallow copy, preventing callers from mutating internal state. The `_pending_post_count` and `_max_pending_post_depth_observed` metrics are also lock-protected. No lock escapes were found.

### `VoiceGatewayClient` as static-method class — design note

All four HTTP methods (`start_session`, `get`, `post`, `delete`, `stop_session`) are `@staticmethod`; the class holds no instance state. This means `VoiceGatewayClient()` is purely a namespace. The pattern is functional but could be a module-level set of functions instead. No bug; noted as a future refactor candidate.

### Backoff implementation in `_assist_loop` — PASS

The `_BACKOFF_STEPS` list is defined inside the method body on every call (not a module constant). This has no correctness impact. The exponential backoff (`backoff_delay * 2` capped at `_BACKOFF_MAX = 4.0`) is correct and has a reasonable ceiling.

### `handle_stop` state write-back — PASS

The stop flow correctly writes the final state back under `self._lock` at line 438–439, after the potentially long summary POST completes. During the summary window, `self._state["active"]` is already `False` (set at line 369), so the `_assist_loop` will exit within 1–1.5 s of the stop call. The second lock acquisition at the end merges summary/gateway-stop fields without risk of stale-state overwrite because the intermediate `state` dict was a copy of `self._state` taken at the start.
