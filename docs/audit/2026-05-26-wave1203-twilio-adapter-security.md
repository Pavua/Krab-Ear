# W1203 Security Audit — `twilio_adapter.py`

**Date:** 2026-05-26  
**Branch:** `audit/twilio-adapter-security-W1203`  
**File audited:** `KrabEar/backend/twilio_adapter.py`  
**Sister audit:** W1195 (telnyx_adapter.py)  
**Severity scale:** HIGH / MED / LOW

---

## Summary

5 findings (2 HIGH, 2 MED, 1 LOW).  
All 5 W1195 Telnyx findings have direct parallels here; no Twilio-specific  
injection vectors found (TwiML is hardcoded, no user input reaches XML).

---

## Finding 1 — HIGH: Unbounded `Retry-After` sleep (DoS / hang)

**Lines:** 199–200

```python
retry_after = resp.headers.get("Retry-After")
wait = float(retry_after) if retry_after else _RATE_LIMIT_SLEEP_SEC
logger.warning("Twilio rate limit hit, waiting %.1fs", wait)
time.sleep(wait)
```

A Twilio-controlled (or MITM-injected) `Retry-After: 86400` header causes the
backend thread to sleep for 24 hours, effectively hanging the IPC worker.

**Fix:** cap at a sane maximum, e.g. `wait = min(float(retry_after), 30.0)`.

---

## Finding 2 — HIGH: `call_control_id` path traversal in URL

**Lines:** 316, 337

```python
result = self._post(f"/Calls/{call_control_id}.json", {"Status": "completed"})
result = self._get(f"/Calls/{call_control_id}.json")
```

`call_control_id` is accepted from the caller without sanitization.
A value such as `"../Accounts/ACother/Calls/CA123"` (relative) or any
string containing `..`, `/`, or `?` is interpolated directly into the URL path
sent to Twilio via `requests`.  Depending on the HTTP client's URL normalization
this can redirect the request to an unintended Twilio resource or leak the
auth credentials to a different account endpoint.

Twilio Call SIDs always match `CA[0-9a-f]{32}` — enforce that regex before use.

**Fix:**
```python
_CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")

def _validate_call_sid(sid: str) -> bool:
    return bool(_CALL_SID_RE.match(sid or ""))
```
Reject the request with `{"ok": False, "error": "invalid_call_sid"}` if it fails.

---

## Finding 3 — HIGH: `account_sid` path traversal in base URL

**Line:** 115

```python
def _base_url(self) -> str:
    return f"{TWILIO_API_BASE}/{self._account_sid}"
```

`_account_sid` is stored as-is (only `.strip()` applied at line 87).  A value
containing `../` or `?key=val` is concatenated into every request URL, potentially
redirecting all API calls to a different Twilio account or leaking credentials.

Twilio Account SIDs always match `AC[0-9a-f]{32}`.  Validate at `__init__` time.

**Fix:**
```python
_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")

# In __init__, after strip:
if self._account_sid and not _ACCOUNT_SID_RE.match(self._account_sid):
    raise ValueError(f"Invalid Twilio Account SID: {self._account_sid!r}")
```

This is Twilio-specific and has no direct Telnyx equivalent (Telnyx uses a
Bearer token, not a path-embedded SID).

---

## Finding 4 — MED: `webhook_url` / `StatusCallback` SSRF

**Lines:** 279–281

```python
if webhook_url:
    payload["StatusCallback"] = webhook_url
    payload["StatusCallbackMethod"] = "POST"
```

`webhook_url` is accepted from IPC callers without validation.  Twilio will
POST call-status events to whatever URL is supplied.  An attacker who controls
this parameter can cause Twilio to act as a proxy and deliver POST requests
(with call metadata) to internal services (`http://169.254.169.254/…`,
`http://localhost:5005/…`, etc.) that are otherwise unreachable.

**Fix:** Validate that `webhook_url` starts with `https://` and is not a private
IP range before adding it to the payload.

```python
from urllib.parse import urlparse

def _is_safe_callback_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme == "https" and bool(p.netloc)
    except Exception:
        return False
```

---

## Finding 5 — MED: Raw API error body forwarded to caller / logged

**Lines:** 222–229

```python
try:
    body = resp.json()
    detail = body.get("message", str(body))
    twilio_code = body.get("code", "")
except ValueError:
    detail = resp.text or f"HTTP {status}"
    twilio_code = ""

logger.error("Twilio API error %s: %s", status, detail)
return {
    "ok": False,
    ...
    "message": detail,
    ...
}
```

Twilio error responses can contain PII (phone numbers, account details) and
internal identifiers.  Forwarding `detail` verbatim to the IPC caller and into
log records risks leaking this data into log files (potentially shipped to
Sentry) and back to the frontend.

Same pattern exists in the `400` handler (lines 164–177): `detail = body.get("message", resp.text)`.

**Fix:** Log the raw detail at DEBUG level only; return a generic sanitized
message at ERROR/caller level.  Whitelist specific Twilio error codes
(`20003`, `21210`, etc.) to safe human-readable strings, falling back to
`"Twilio API error (code {twilio_code})"`.

---

## Non-findings (Twilio-specific checks)

| Check | Result |
|---|---|
| TwiML XML injection | NOT a finding — TwiML is hardcoded `"<Response><Say>Connected</Say></Response>"` (line 277); no user input reaches XML |
| Twilio X-Twilio-Signature validation | N/A — adapter only *initiates* outbound calls; incoming webhook validation belongs to the receiving HTTP server (not implemented here) |
| Auth token logged | NOT found — `_auth_token` never appears in log statements |
| Basic Auth vs Bearer | Correct — Twilio REST v2010 uses Basic Auth (Account SID + Auth Token); implementation matches spec |
| Subaccount handling | No subaccount support present; single-account only; not a security issue |

---

## `http://` adapter mount (LOW)

**Line:** 56 in `_build_session()`

```python
session.mount("http://", adapter)
```

The `TWILIO_API_BASE` constant is hardcoded to `https://`, so no plaintext
request can originate from normal code paths.  However, the `http://` mount
is unnecessary and could enable plaintext requests if the base URL were ever
changed.  Remove the `http://` mount to reduce attack surface.

---

## Fix Priority

| # | Severity | Finding | Fix effort |
|---|---|---|---|
| 1 | HIGH | Unbounded Retry-After sleep | 1 line |
| 2 | HIGH | call_control_id path traversal | +regex + guard in 2 methods |
| 3 | HIGH | account_sid path traversal (Twilio-specific) | +regex in `__init__` |
| 4 | MED | webhook_url SSRF | +URL validator |
| 5 | MED | Raw error body in logs/IPC | sanitize detail before log/return |
| — | LOW | Unnecessary `http://` adapter mount | remove 1 line |
