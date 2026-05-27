# Audit W1204 — VGWebSocketClient Security Review

**File**: `KrabEar/backend/vg_ws_client.py`  
**Date**: 2026-05-26  
**Auditor**: W1204 (sub-agent, read-only)  
**Scope**: WebSocket security of `VGWebSocketClient` — TLS, auth, reconnect, DoS, path traversal, PII leakage.

---

## Summary

5 findings (2 HIGH, 2 MEDIUM, 1 LOW). No certificate pinning is expected for a localhost-default gateway; the critical concerns are unverified TLS on remote wss:// targets, uncapped reconnect storms, unbounded inbound frame size, path traversal in `session_id`, and API key exposure in error context.

---

## Findings

### F1 — HIGH: No TLS certificate verification on `wss://` connections

**Location**: `vg_ws_client.py:41`

```python
async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
```

`websockets.connect()` accepts an `ssl` parameter. When the URL scheme is `wss://`, the library creates a default `SSLContext` with `check_hostname=True` and `verify_mode=ssl.CERT_REQUIRED` **only when `ssl=True` is passed explicitly or when the library internally detects `wss://`**. In `websockets >= 12.0` the library does auto-upgrade to TLS for `wss://` URLs, but the caller never passes a custom `ssl.SSLContext`, so it relies entirely on the OS/Python default CA bundle.

The deeper problem: no code path validates that the `wss://` cert belongs to the expected gateway host. A MITM on any network segment between the client and a remote VG endpoint can present a cert signed by any trusted CA, impersonate the Voice Gateway, and receive all transcription event streams (which contain sensitive audio metadata). There is no certificate pinning and no explicit `ssl=ssl.create_default_context()` that would let the code fail-fast on untrusted certs in CI/test environments where system CAs are absent.

**Risk**: Interception of all streamed transcription events when `gateway_url` resolves to a remote host.

**Fix**: Pass `ssl=ssl.create_default_context()` explicitly so the call never silently falls back to a no-verify context. For production remote gateways, add SPKI-pinning via a custom `SSLContext`. Document that `ws://` (plain) is only safe for `127.0.0.1`.

---

### F2 — HIGH: `session_id` injected into URL without sanitisation — path traversal

**Location**: `vg_ws_client.py:28`

```python
self.ws_url = f"{ws_base.rstrip('/')}/v1/sessions/{session_id}/stream"
```

`session_id` is accepted as a plain `str` from the caller (IPC param) with no validation. A value such as `../admin/stream`, `%2F..%2Fadmin%2F`, or `../../config/dump` would silently construct a URL that points to an unintended endpoint on the Voice Gateway server. If the gateway uses the path to authorise the session, a crafted `session_id` can bypass authorisation or access other sessions' streams.

The `settings_validator.py` whitelist on `voice_gateway_url` does not protect `session_id` — it is a separate parameter.

**Risk**: Path traversal on the VG server; potential access to other sessions' streams or admin endpoints.

**Fix**: Validate `session_id` at construction time with a strict allowlist pattern, e.g.:
```python
import re
_SESSION_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,128}$')
if not _SESSION_ID_RE.fullmatch(session_id):
    raise ValueError(f"Invalid session_id: {session_id!r}")
```

---

### F3 — MEDIUM: Reconnect backoff cap too low — reconnect storm risk

**Location**: `vg_ws_client.py:19-20, 69`

```python
_RECONNECT_BASE_SEC = 1.0
_RECONNECT_MAX_SEC = 10.0
...
backoff = min(backoff * 2, _RECONNECT_MAX_SEC)
```

The reconnect loop caps at **10 seconds**. There is no jitter and no total-failure ceiling (the loop runs forever while `_stop` is unset). If the Voice Gateway is down or unreachable, the client will fire a new TCP/TLS handshake every 10 seconds indefinitely, generating:

1. Continuous `WARNING` log spam (visible in prod logs, masking real errors).
2. A `_push_error("vgw.reconnect", ...)` call on every iteration — potential error-bus flooding.
3. In a multi-session scenario (multiple `VGWebSocketClient` instances), an amplified connection storm to the gateway.

The `_RECONNECT_MAX_SEC = 10` cap is the default value for many internal tools, but Voice Gateway streams are long-lived; a 10 s max is appropriate for LAN, but insufficient for remote/cloud deployments where the gateway might be down for minutes.

**Risk**: Log flood, error-bus saturation, gateway overload during outages.

**Fix**: Increase `_RECONNECT_MAX_SEC` to 60–300 s for remote deployments; add `±30%` random jitter; add a max-attempts counter (e.g. 20 retries) after which the client calls `self.stop()` and emits a critical error. Example:

```python
_RECONNECT_MAX_SEC = 60.0
_RECONNECT_MAX_ATTEMPTS = 20

import random
jitter = random.uniform(0.75, 1.25)
backoff = min(backoff * 2 * jitter, _RECONNECT_MAX_SEC)
```

---

### F4 — MEDIUM: No inbound frame size limit — DoS via large WebSocket message

**Location**: `vg_ws_client.py:41`

```python
async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
```

`websockets.connect()` defaults to `max_size=2**20` (1 MiB) in `websockets >= 10.0`. However this default is library-version-dependent and is not explicitly set in the code. A rogue or compromised Voice Gateway could send a single oversized frame (e.g. 512 MiB) which the library would buffer entirely in memory before passing it to `json.loads()`. Given that Krab Ear targets M4 Max with 36 GB RAM, a single frame won't crash the process, but repeated large frames could spike memory and starve the STT/MLX pipeline.

There is also no limit on the number of frames processed per second — no rate-limiting on the inbound event loop.

**Risk**: Memory exhaustion / latency spike if the gateway (or a MITM on an unverified connection) sends oversized frames.

**Fix**: Explicitly set `max_size` to a safe limit:
```python
async with websockets.connect(
    self.ws_url,
    extra_headers=headers,
    max_size=256 * 1024,  # 256 KiB — sufficient for JSON event payloads
) as ws:
```

---

### F5 — LOW: API key and full `ws_url` written to error context — potential PII/secret leakage

**Location**: `vg_ws_client.py:42, 94`

```python
logger.info("VG WS connected: %s", self.ws_url)          # line 42
...
context={"session_id": self.session_id, "ws_url": self.ws_url},  # line 94 → error_bus
```

`self.ws_url` is constructed from `gateway_url` which may contain query parameters with tokens in future refactors (e.g. `?token=xyz`). More critically, `self.ws_url` is logged at `INFO` level unconditionally, so when `LOG_FORMAT=json` is enabled for production, every reconnect emits the full URL (including host, port, session path) to the structured log — which may be shipped to Sentry or a third-party log aggregator.

Additionally, `_push_error` bundles `ws_url` in the `context` dict that flows into the `ErrorBus` and potentially into Sentry breadcrumbs (per `observability.py` breadcrumb pattern). The `api_key` itself is not logged directly, but if a future change adds `Authorization` header logging (e.g. a debug mode) the key would leak.

**Risk**: Session IDs and gateway URLs reaching log aggregators / Sentry events; future risk of API key leakage if debug logging is added.

**Fix**:
- Redact `ws_url` to host+path only (strip query params) before logging: `urllib.parse.urlunparse(parsed._replace(query="", fragment=""))`.
- In `_push_error` context, include only `session_id` and redacted host, not the full `ws_url`.
- Add `api_key` to a `_REDACTED_FIELDS` list in `settings_backup.py` (already exists for other secrets) to prevent backup exposure.

---

## Non-Findings (audited, not flagged)

| Check | Result |
|---|---|
| API key in URL | API key correctly sent as `Authorization: Bearer` header, not in URL |
| Origin header | Not applicable — `websockets` does not set `Origin` for non-browser clients; server-side origin validation is VG's responsibility |
| Subprotocol whitelist | No `subprotocols=` passed; VG negotiation is implicit — acceptable for a private API |
| Concurrent connection limit | Single `VGWebSocketClient` per call session; no pooling code exists |
| Token refresh on long sessions | No refresh mechanism, but `api_key` is a static bearer token — acceptable if short-lived sessions; long-lived sessions should rotate via IPC `set_settings` |

---

## Recommended Priority

| # | Severity | Finding |
|---|---|---|
| F2 | HIGH | `session_id` path traversal — fix before any remote VG deployment |
| F1 | HIGH | No explicit TLS context — fix before remote wss:// usage |
| F3 | MEDIUM | Reconnect storm / missing jitter |
| F4 | MEDIUM | Unbounded inbound frame size |
| F5 | LOW | URL/session_id in logs and error context |
