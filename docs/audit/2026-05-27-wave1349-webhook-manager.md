# W1349 — WebhookManager audit

**Date:** 2026-05-27  
**File:** `KrabEar/backend/webhook_manager.py`  
**Auditor:** W1349 (sub-agent, read-only)  
**Branch:** `audit/webhook-manager-W1349`

---

## Summary

5 findings. No critical blockers — the module is well-structured with working SSRF guard,
HMAC signing, timeout enforcement, and retry policy. Issues are medium-severity hardening
gaps: redirect-based SSRF bypass, missing response-body size cap, no `ssl.SSLContext`
passed (uses Python default which is fine, but not explicit), no `privacy_mode` gate on
`fire_webhook`, and one stale test that contradicts the actual SSRF behavior.

---

## Findings

### F1 — SSRF via HTTP redirect (MEDIUM)

**Location:** `_post_once` (line 375–380), uses `urllib.request.urlopen`.

`urlopen` installs `HTTPRedirectHandler` by default and follows 3xx redirects automatically.
An attacker who can register a webhook (via `allow_local=False` path) pointing to a
public URL that then redirects to `http://127.0.0.1/...` or `http://192.168.x.x/...` will
bypass the SSRF guard entirely — the guard only checks the *registered* URL, not redirect
destinations.

**Reproduction sketch:**
```
register_webhook("https://attacker.com/redirect-to-localhost", events=[])
# attacker.com returns: 302 Location: http://127.0.0.1:5005/internal-endpoint
# _post_once follows the redirect, request lands on localhost
```

**Fix:** Build a custom opener that raises on any redirect:
```python
import urllib.request

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disallowed", headers, fp)

_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())
# Replace urlopen(req, ...) with _NO_REDIRECT_OPENER.open(req, timeout=...)
```

---

### F2 — No response body size limit (LOW)

**Location:** `_post_once` (line 377–380).

```python
with urlopen(req, timeout=_REQUEST_TIMEOUT_SEC) as resp:
    return resp.status
```

The response body is never read — only `resp.status` is accessed. While this avoids
unbounded memory growth in most cases, it does **not** call `resp.read()` with a cap, so
the socket is held open until Python's HTTP keep-alive machinery closes it. On a slow or
adversarial server this can tie up a daemon thread for the full `_REQUEST_TIMEOUT_SEC`
(10 s) per attempt × 3 retries = 30 s per webhook, with no defence against a server that
streams a huge body slowly.

Additionally, if a future maintainer adds response-body inspection (e.g. to log the error
message on 4xx), there is no guard in place.

**Fix:** Add an explicit bounded read inside `_post_once`:

```python
_MAX_RESPONSE_BYTES = 4096

with urlopen(req, timeout=_REQUEST_TIMEOUT_SEC) as resp:
    resp.read(_MAX_RESPONSE_BYTES)   # drain/discard; cap prevents runaway
    return resp.status
```

---

### F3 — `fire_webhook` not gated on `privacy_mode` (LOW)

**Location:** `fire_webhook` (line 236–263); `BackendService.__init__` (service.py line 394).

`WebhookManager` has no reference to settings. When `privacy_mode_enabled=True`, other
data-exfiltration paths (export_history_srt, export_history_csv, export_history_json —
service.py lines 3656, 3703, 3748) all return a `privacy_mode` error immediately. But
`fire_webhook` is not called from service.py at all yet (no call sites found in the
backend), so the gap is theoretical today. However, the first caller that wires
`fire_webhook` to real STT/translation events will silently exfiltrate transcript data
even with privacy mode on.

**Fix options:**
1. Pass a `privacy_mode_getter: Callable[[], bool]` into `WebhookManager.__init__` and
   short-circuit `fire_webhook` when it returns `True`.
2. Gate at the call site(s) in `service.py` before invoking `fire_webhook`.

Option 2 is lower coupling. Either way, add a test asserting `fire_webhook` sends zero
requests when privacy mode is active.

---

### F4 — Stale test documents wrong behavior (LOW / test correctness)

**Location:** `KrabEar/tests/test_webhook_manager.py`, lines 619–630,
`WebhookManagerURLValidationTestCase.test_localhost_http_currently_accepted`.

The test comment says:
> "WebhookManager does NOT block localhost URLs by scheme check alone — the current
> implementation only validates http/https prefix."

But this is wrong: `_is_safe_webhook_url` **does** block localhost via `_BLOCKED_HOSTNAMES`
(line 71–73 of `webhook_manager.py`). The test itself confirms this by currently **failing**:

```
ValueError: URL отклонён защитой SSRF (localhost/empty host blocked ('localhost'))
```

Running the suite produces `1 failed, 70 passed`. The comment and the `assertIsNotNone(wid)`
assertion are a leftover from a pre-SSRF-guard version of the test. The test should be
updated to `assertRaises(ValueError)` (matching the parallel test in
`test_webhook_ssrf_guard.py::SSRFGuardRegisterTestCase.test_register_localhost_raises`).

**Fix:** Update `test_localhost_http_currently_accepted`:

```python
def test_localhost_http_rejected(self) -> None:
    """localhost http:// отклоняется SSRF guard."""
    with self.assertRaises(ValueError):
        self._mgr.register_webhook("http://localhost:9999/hook", events=[])
```

---

### F5 — `allow_local` bypass is IPC-controllable without auth (LOW)

**Location:** `handle_register_webhook` (lines 290–301).

```python
allow_local: bool = bool(params.get("webhook_allow_local", False))
webhook_id = self.register_webhook(..., allow_local=allow_local)
```

Any IPC client that can call `register_webhook` can pass `webhook_allow_local: true` and
register a webhook pointing at `127.0.0.1`, fully bypassing the SSRF guard. The IPC socket
(`krabear.sock`) is accessible by any local process running as the same user, so this is
a local privilege concern rather than a remote one — but it is architecturally inconsistent:
a guard that the caller can disable on request provides no real protection against a
malicious local process.

The intent documented in code comments is that `allow_local` is for "self-hosted
environments" — a legitimate dev use-case. A tighter design would require this flag to be
set via a settings key (`webhook_allow_local` in `settings.json`, writable only by the
user) rather than passed per-request from an arbitrary IPC caller.

**Fix:** Remove `webhook_allow_local` from the per-request params; instead read it from
`BackendService._get_runtime_setting("webhook_allow_local", False)` and pass that cached
value into `WebhookManager` at construction (or per-call via a settings accessor).

---

## Coverage status

| Area | Covered | Notes |
|------|---------|-------|
| SSRF localhost/RFC1918/link-local | Yes | `test_webhook_ssrf_guard.py` 26 tests |
| HMAC signing | Yes | `test_webhook_manager.py` tests 16–17 |
| Retry on 5xx, no-retry on 4xx | Yes | tests 18–19 |
| Timeout graceful handling | Yes | tests 33–34 |
| Persistence reload | Yes | test 8 |
| Secret not exposed in list | Yes | tests 9–10 |
| Disabled webhook skipped | Yes | tests 31–32 |
| Redirect SSRF bypass | **No** | F1 — not tested |
| Response body size cap | **No** | F2 — no cap exists |
| Privacy mode interaction | **No** | F3 — no test |
| Stale localhost test fixed | **No** | F4 — test fails |
| `allow_local` per-request bypass | **No** | F5 — no negative test |

---

## Fix priority

| # | Severity | Effort | Recommended action |
|---|----------|--------|-------------------|
| F1 | Medium | Low | Add `_NoRedirectHandler` custom opener |
| F4 | Low | Trivial | Fix stale test (1-line change) |
| F2 | Low | Low | Add `resp.read(_MAX_RESPONSE_BYTES)` |
| F3 | Low | Medium | Gate `fire_webhook` on privacy setting |
| F5 | Low | Medium | Move `allow_local` to settings, remove from IPC params |
