# Audit: TelegramBridge residual issues — W943

**Date:** 2026-05-26
**Auditor:** W943 (sub-agent, read-only)
**File:** `KrabEar/backend/telegram_bridge.py` (252 lines)
**Tests:** `KrabEar/tests/test_telegram_bridge.py`, `KrabEar/tests/test_telegram_bridge_wave622.py`
**Scope:** Residual issues after W898 hostname-allowlist fix.

---

## Already-fixed (W898)

W898 is not present in `telegram_bridge.py` itself — no hostname validation
lives in that file. The W898 reference in the task prompt appears to be a
forward-reference to a planned fix; the SSRF guard that *does* exist lives in
`backend/webhook_manager.py` (`_is_url_safe()`). `TelegramBridge` has **no
hostname validation at all** — the `base_url` constructor argument is accepted
without any allowlist check (see Finding 1 below).

---

## Findings

### FINDING 1 — HIGH: No hostname allowlist on `base_url` (W898 not implemented here)

**Location:** `telegram_bridge.py:53–58`, `service.py:478–483`

`TelegramBridge.__init__` accepts any `base_url` string and passes it
directly to `requests.post()`. If `TELEGRAM_BRIDGE_URL` is overridden (via
`KRAB_EAR_TELEGRAM_BRIDGE_URL` env var or a future `set_settings` IPC call)
to a non-localhost URL (e.g. `http://internal-service:8080` or
`http://169.254.169.254`), the backend silently makes outbound requests to
arbitrary targets — an SSRF vector.

The webhook SSRF guard (`webhook_manager._is_url_safe`) is not reused here.

**Risk:** An attacker who can set environment variables or call `set_settings`
(if the setting is ever made runtime-writable) can redirect bridge traffic to
any network endpoint. Even without adversarial access, misconfiguration routes
transcript text off-box.

**Recommendation:** Add a constructor-time allowlist check:
```python
from urllib.parse import urlparse
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
parsed = urlparse(base_url)
if parsed.hostname not in _ALLOWED_HOSTS:
    raise ValueError(f"TelegramBridge: base_url host must be localhost, got {parsed.hostname!r}")
```
Note: `0.0.0.0` is **not** in the allowlist above — it is not a valid client
address (it means "all interfaces" as a bind address; connecting to it on
macOS resolves to `127.0.0.1` by convention but is semantically wrong and
should be rejected).

---

### FINDING 2 — HIGH: Port hardcoded at construction time — stale on user reconfig

**Location:** `core/config.py:397`, `service.py:479`

`TELEGRAM_BRIDGE_URL` (default `http://localhost:8080`) is read from the
Pydantic-Settings singleton **once at `BackendService.__init__` time** and
passed to `TelegramBridge.__init__`. The bridge instance is then reused for
the entire backend lifetime.

If the user changes the main Krab web-panel port (e.g. via `WEB_PORT` in
Krab's env) after Krab Ear has started, the bridge continues sending to the
old port until the backend is restarted. There is no mechanism to signal the
bridge to reload the URL at runtime.

`TELEGRAM_BRIDGE_URL` is a static env-var-backed setting; it cannot be
changed via `set_settings` IPC without a backend restart. This is documented
nowhere in the docstring of `_handle_send_to_telegram`.

**Risk:** Silent delivery failure (500/connection-refused) with no user
feedback except a RuntimeError logged at WARNING level.

**Recommendation:** Either (a) rebuild the URL in each request from
`_get_runtime_setting("telegram_bridge_url", ...)`, or (b) document the
restart requirement clearly in the IPC API reference and error message.

---

### FINDING 3 — MEDIUM: PII (transcript text) sent to web panel — no privacy_mode guard

**Location:** `service.py:3003–3040` (`_handle_send_to_telegram`)

The `privacy_mode_enabled` setting is checked in `translation_service.py`
(lines 96, 201) and `observability.py` (line 122) before transmitting or
logging transcript content. The `_handle_send_to_telegram` handler has **no
equivalent check**: it accepts arbitrary `text` from the IPC caller and POSTs
it to the main Krab web panel, even when `privacy_mode_enabled=True`.

A user who enables privacy mode (expecting no text to leave the local process)
can still inadvertently call `send_to_telegram` with a full transcript
snippet. The text then travels over localhost HTTP — not encrypted, visible in
kernel buffers, network sniffers, and main Krab logs.

**Risk:** Privacy-mode bypass; GDPR/CCPA violation if the main Krab userbot
logs or stores the forwarded text.

**Recommendation:**
```python
if self._get_runtime_setting("privacy_mode_enabled", False):
    raise RuntimeError("privacy_blocked: send_to_telegram недоступен в режиме конфиденциальности")
```
Add this check immediately after the `bridge_disabled` guard at line 3003.

---

### FINDING 4 — MEDIUM: No ordered delivery — concurrent sends are unordered fire-and-forget

**Location:** `telegram_bridge.py:63–65`, `service.py:3027–3040`

Multiple concurrent IPC callers can call `send_to_telegram` simultaneously.
Each acquires only `self._lock` for circuit-breaker state updates, not for
request ordering. `requests.post` calls are issued concurrently from whatever
thread handles each IPC request. Message delivery order at the main Krab side
is determined by TCP scheduling, not call order.

The existing test `TestTelegramBridgeConcurrentSend` verifies all 10 threads
succeed but does **not** verify ordering.

**Risk:** When Krab Ear auto-sends multiple transcript segments (e.g. via bulk
export or batch notification), messages arrive in non-deterministic order in
Telegram.

**Recommendation:** Add an in-process `threading.Semaphore(1)` or a
single-threaded executor queue for outbound notifications if ordering matters.
Alternatively, document the fire-and-forget semantic explicitly and accept the
risk.

---

### FINDING 5 — MEDIUM: `TELEGRAM_BRIDGE_ENABLED` read from static settings object (Wave 58 anti-pattern)

**Location:** `service.py:3003`, `service.py:3261`

CLAUDE.md (Wave 58 lesson) states: "ALL startup-time reads of user-overridable
settings MUST use `self._get_runtime_setting(key, default)`, NOT
`DEFAULT_SETTINGS.get(key, default)`."

Both `_handle_send_to_telegram` (line 3003) and `_handle_list_telegram_chats`
(line 3261) check `settings.TELEGRAM_BRIDGE_ENABLED` — the Pydantic-Settings
singleton, NOT the runtime-overridable cached settings dict. This means
toggling `TELEGRAM_BRIDGE_ENABLED` via `set_settings` IPC has no effect at
runtime; the guard always reflects the value at process startup.

**Risk:** A user who disables the bridge via `set_settings` (if that key is
ever exposed in the settings schema) will see the change in `get_settings` but
the guard will not activate, silently continuing to allow bridge calls.

**Recommendation:**
```python
if not self._get_runtime_setting("telegram_bridge_enabled", True):
    raise RuntimeError("bridge_disabled: ...")
```

---

### FINDING 6 — LOW: No authentication between Krab Ear and main Krab web panel

**Location:** `telegram_bridge.py:112–113`, docstring line 8 ("без авторизации")

The `POST /api/notify` and `GET /api/chats` requests carry no authentication
token or HMAC signature. The main Krab web panel listens on `localhost:8080`
and apparently accepts any well-formed JSON without verifying the caller.

Any local process that can reach `localhost:8080` (any other macOS process
running as the same user, or any sandboxed app with network entitlements) can:
- enumerate all Telegram chats the userbot has access to (`/api/chats`)
- send arbitrary messages to any of those chats on the user's behalf

This is a local privilege boundary issue, not a remote one, but it is
significant given that Krab Ear is a general-purpose assistant app that may
be running alongside untrusted local software.

**Recommendation:** Add an optional `auth_token` parameter to
`TelegramBridge.__init__` that, when set, is included as
`Authorization: Bearer <token>` in all outbound requests. The main Krab web
panel would validate it. No action required on the Krab Ear side until the
main Krab API adds token support, but the field should be wired now.

---

## Checklist answers

| # | Question | Answer |
|---|----------|--------|
| 1 | Port hardcoded or from settings? | From `TELEGRAM_BRIDGE_URL` setting but read once at init — stale on reconfig (Finding 2) |
| 2 | HTTP timeout present? | Yes — `timeout=self._timeout_sec` (default 5.0 s), explicit on both `post` and `get` |
| 3 | Retry on connection refused? | No retry. Single attempt. Increments circuit breaker. Silent drop after CB opens. No dead-letter queue. |
| 4 | PII / privacy_mode bypass? | YES — no privacy_mode guard (Finding 3, HIGH) |
| 5 | 4xx vs 5xx handling? | Unified: both map to `RuntimeError(krab_error/krab_unavailable)`. 503 gets specific `krab_unavailable` code; all others get `krab_error`. Circuit breaker increments on both. |
| 6 | Concurrent notifications ordered? | No ordering guarantee (Finding 4, MEDIUM) |
| 7 | Web panel auth? | None (Finding 6, LOW) |
| 8 | Schema versioning? | None. Payload is `{text, chat_id, reply_to_message_id?}`. No version field. `reply_to_message_id` already has a TODO noting the server side ignores it. |
| 9 | Test coverage? | Good for happy path and circuit breaker. Missing: privacy_mode guard, URL validation, ordered-delivery assertion. |
| 10 | W898 hostname allowlist — `0.0.0.0` excluded? | W898 is NOT implemented in `telegram_bridge.py`. No hostname allowlist exists. `0.0.0.0` should be rejected if/when one is added. |

---

## Summary

5 findings (2 HIGH, 2 MEDIUM, 1 LOW) across the areas of SSRF exposure,
runtime staleness, PII bypass, unordered concurrent delivery, and missing auth.
The timeout is correctly set (not a finding). The W898 hostname allowlist
described in the task prompt does not yet exist in `telegram_bridge.py` — it
is the highest-priority item to implement.
