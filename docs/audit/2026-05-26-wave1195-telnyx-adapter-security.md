# Security Audit: TelnyxAdapter — W1195

**File:** `KrabEar/backend/telnyx_adapter.py`
**Date:** 2026-05-26
**Wave:** W1195
**Auditor:** sub-agent W1195

---

## Summary

5 findings. 2 HIGH, 2 MEDIUM, 1 LOW. No API key logged in plain text. TLS verification is not
explicitly disabled. Stub-mode is correctly guarded. Primary concerns: unbounded sleep from
attacker-controlled `Retry-After` header, path traversal via unvalidated `call_control_id`,
unauthenticated webhook URL acceptance, `http://` scheme allowed in `api_base`, and raw
`resp.text` returned to callers without sanitization.

---

## Findings

### F1 — HIGH: Unbounded `time.sleep` from attacker-controlled `Retry-After` header

**Location:** `_handle_response`, lines 173–176

```python
retry_after = resp.headers.get("Retry-After")
wait = float(retry_after) if retry_after else _RATE_LIMIT_SLEEP_SEC
logger.warning("Telnyx rate limit hit, waiting %.1fs", wait)
time.sleep(wait)
```

**Risk:** The 429 handler trusts the `Retry-After` response header without any upper bound. A
compromised or MITM'd Telnyx endpoint (or a DNS-spoofed `api.telnyx.com`) can return
`Retry-After: 86400` and freeze the IPC worker thread for 24 hours, causing a denial-of-service
of the entire backend. Since `_handle_response` is called on the IPC request thread, the hang
blocks all subsequent IPC calls until the sleep ends.

Additionally, the `urllib3.util.retry.Retry` adapter also retries on 429 (it is in
`_RETRY_STATUS`), meaning the sleep can stack with the automatic retry backoff.

**Fix:** Cap the sleep: `wait = min(float(retry_after), 60.0) if retry_after else _RATE_LIMIT_SLEEP_SEC`.
Also remove 429 from `_RETRY_STATUS` to prevent double-sleep (the manual sleep in
`_handle_response` already handles rate limiting).

---

### F2 — HIGH: `call_control_id` interpolated into URL path without sanitization (path traversal)

**Location:** `hangup` (line 279), `get_call_status` (line 299)

```python
# hangup:
result = self._post(f"/calls/{call_control_id}/actions/hangup", {})

# get_call_status:
result = self._get(f"/calls/{call_control_id}")
```

**Risk:** `call_control_id` originates from the Telnyx API response (`data.get("call_control_id",
"")`), from IPC caller params, or from `CallSessionStore`. If a malformed value containing `/` or
`..` reaches these methods — e.g. `../../other-resource` — the constructed URL would target a
different Telnyx API endpoint. In a MITM or supply-chain scenario where a malicious Telnyx API
response injects a crafted `call_control_id`, this could cross Telnyx resource boundaries and
invoke unintended API actions on the caller's account.

Only an empty-string check is present; there is no alphanumeric/UUID format validation.

**Fix:** Add a regex guard before use:
```python
_CALL_CONTROL_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{8,256}$')

if not _CALL_CONTROL_ID_RE.match(call_control_id):
    return {"ok": False, "error": "invalid_call_control_id"}
```

---

### F3 — MEDIUM: `webhook_url` accepted without SSRF validation

**Location:** `dial`, lines 245–246

```python
if webhook_url:
    payload["webhook_url"] = webhook_url
```

**Risk:** The `webhook_url` parameter is forwarded verbatim to Telnyx with no URL validation.
`webhook_url` appears in the public `CallProvider` interface and is passed from the Swift layer
through IPC. An attacker who can call the `call_session_create` IPC method (local Unix socket,
authenticated only by process ownership) could supply `webhook_url=http://169.254.169.254/latest/
meta-data/` (SSRF via Telnyx's outbound callback), or a `file://` or `javascript:` scheme.

Note: `webhook_manager.py` already has `_is_safe_webhook_url()` implementing SSRF guards. That
function is not reused here.

**Fix:** Import and call `_is_safe_webhook_url` before forwarding:
```python
from backend.webhook_manager import _is_safe_webhook_url

if webhook_url:
    safe, reason = _is_safe_webhook_url(webhook_url)
    if not safe:
        return {"ok": False, "error": "invalid_webhook_url", "message": reason}
    payload["webhook_url"] = webhook_url
```

---

### F4 — MEDIUM: `api_base` allows `http://` override — API key sent in plaintext

**Location:** `__init__` (line 88), `_build_session` (line 55)

```python
self._api_base = api_base.rstrip("/")   # no scheme check
# ...
session.mount("http://", adapter)       # http:// adapter mounted
```

**Risk:** `api_base` is a constructor parameter with default `TELNYX_API_BASE =
"https://api.telnyx.com/v2"` but no enforcement that it must be HTTPS. If
`call_provider_factory.py` ever passes a configurable `api_base` (or a test/dev fixture passes
`http://localhost/...`), the Bearer token in the `Authorization` header will be transmitted in
plaintext. The `http://` adapter mount in `_build_session` makes this trivially reachable.

**Fix:**
1. Validate `api_base` starts with `https://` in `__init__` and raise `ValueError` otherwise.
2. Remove the `session.mount("http://", adapter)` line — it serves no purpose for a Telnyx-only
   client and silently enables plaintext credential transmission.

---

### F5 — LOW: Raw `resp.text` returned in error `message` field — potential sensitive data exposure

**Location:** `_handle_response`, lines 196–200

```python
detail = errors[0].get("detail", str(errors)) if errors else resp.text
# ...
detail = resp.text or f"HTTP {status}"
logger.error("Telnyx API error %s: %s", status, detail)
return {
    "ok": False,
    "error": f"http_{status}",
    "message": detail,  # raw Telnyx response body
    ...
}
```

**Risk:** Telnyx API error bodies can include authentication hints, account identifiers, or
partial request echo. Returning raw `resp.text` in the IPC response propagates this verbatim
to the Swift UI and to any logs that record IPC responses. While `resp.text` is unlikely to
contain the Bearer token itself (Telnyx does not echo request headers), it may include
`connection_id`, `from_number`, account-level metadata, or internal Telnyx resource URIs.

**Fix:** Limit the returned detail to the `errors[*].detail` string (already extracted), and
truncate to a safe maximum length (e.g. 512 chars):
```python
detail = (errors[0].get("detail") or f"HTTP {status}") if errors else f"HTTP {status}"
detail = detail[:512]
```
Avoid ever logging `resp.text` at `error` level where log aggregators may capture it.

---

## Confirmed-OK Items

| Area | Verdict |
|------|---------|
| TLS verification | No `verify=False` anywhere; default requests behaviour enforces cert validation. |
| API key in logs | `_api_key` is never passed to `logger.*` calls. Error messages reference "TELNYX_API_KEY" by name only in the 401 message, not its value. |
| `settings_backup.py` redaction | `telnyx_api_key` is listed in `_SENSITIVE_FIELDS` — redacted before backup writes. |
| Stub-mode safety | `_configured` checks `bool(self._api_key)` before every public method; empty key unambiguously returns `{"ok": False, "error": "telnyx_not_configured"}`. |
| Retry backoff (non-429) | `backoff_factor=1.0` with 3 retries gives max delay of ~7 s for 5xx — bounded and reasonable. |
| Phone number validation | `_is_valid_phone` enforces E.164 regex before `dial()`. |

---

## Recommended Priority

| # | Finding | Severity | Effort |
|---|---------|----------|--------|
| F1 | Cap `Retry-After` sleep | HIGH | 1 line |
| F2 | Validate `call_control_id` format | HIGH | 3 lines |
| F3 | SSRF guard on `webhook_url` | MEDIUM | 5 lines |
| F4 | Reject non-HTTPS `api_base` + remove `http://` mount | MEDIUM | 4 lines |
| F5 | Truncate raw `resp.text` in error dict | LOW | 2 lines |
