# Audit: observability.py post-fix (W1483)

**Date:** 2026-05-27
**Branch:** audit-observability-post-W1483 (off codex/krab-ear-v2)
**Scope:** `KrabEar/backend/observability.py` + related files
**Auditor:** W1483 sub-agent (read-only)
**Prior waves:** W1193 (original audit), W1199 (F1 HIGH fix), W1201 (F2+F4 fix), W1202 (F3 MEDIUM — OPEN)

---

## Merge State Verification

### W1193 — Original audit (5 findings)
**Status: MERGED** — commit `0f07f430` / PR #1098 (2026-05-27)
Audit document at `docs/audit/2026-05-26-wave1193-observability.md`. All 5 findings documented and referenced by subsequent fix waves.

### W1199 — F1 HIGH fix (privacy_mode_enabled not consulted at startup)
**Status: MERGED** — commit `825b7596` / PR #1109 (2026-05-27)
`service.py main()` now loads settings from disk via `StateStore` BEFORE calling `init_sentry`, so `privacy_mode_enabled=True` is honoured on first process launch. Runtime flip (False→True via IPC) is handled in `settings_service.py` lines 371–384: flushes pending Sentry events, calls `sentry_sdk.init(dsn=None)`, and resets `_sentry_initialized = False`. 10 unit tests in `test_init_sentry_privacy_W1199.py` (all passing).

### W1201 — F2+F4 fix (SDK version pin + before_send PII redaction)
**Status: MERGED** — commit `89edbc74` / PR #1106 (2026-05-27)
`requirements.txt` now pins `sentry-sdk>=2.0,<3.0`. `observability.py` now includes:
- `_sentry_before_send` callback wired via `before_send=_sentry_before_send` in `sentry_sdk.init()`
- `include_local_variables=False` passed to `sentry_sdk.init()`
- `_redact_string` redacts `/Users/<name>/...` → `~/...` and drops `KrabEar/transcripts/...` paths entirely
- 17 unit tests in `test_sentry_before_send_W1201.py` (all passing)

### W1202 — F3 MEDIUM fix (_handle_send_errors_to_sentry sentry_sdk bypass)
**Status: NOT MERGED / NOT FOUND**
No commit found matching W1202 or the F3 fix pattern. The `_handle_send_errors_to_sentry` handler in `service.py` still imports `sentry_sdk` directly (bypassing `observability.py` guards) and calls `sentry_sdk.add_breadcrumb(data=err.context)` without an `is_sentry_initialized()` check. This finding from W1193 remains open.

---

## New Findings (post-fix residuals)

### F1 — MEDIUM: `_sentry_before_send` does not walk `breadcrumbs` field

**File:** `KrabEar/backend/observability.py` — `_sentry_before_send` function
**Description:**
The `_sentry_before_send` callback introduced by W1201 walks: exception stacktrace frames, `extra`, `contexts`, `tags`, and `message`. It does **not** walk the `breadcrumbs` key, which Sentry events also carry. Sentry collects breadcrumbs automatically and from `add_breadcrumb()` calls; these can include `data=` dicts with local file paths.

Confirmed with a live test:
```python
event = {
    'breadcrumbs': {
        'values': [
            {'category': 'ipc', 'data': {'path': '/Users/alice/KrabEar/transcripts/session.md'}},
        ]
    }
}
result = _sentry_before_send(event, None)
# result['breadcrumbs']['values'][0]['data']['path'] == '/Users/alice/KrabEar/transcripts/session.md'
# NOT redacted
```

**Impact:** If any breadcrumb `data=` dict contains a path (e.g. from a caller that logs a file path), it leaks through `before_send` unredacted. The W1201 fix covers stack frames and top-level fields but leaves breadcrumbs uncovered.

**Fix:** Add `breadcrumbs` to `_sentry_before_send` walk:
```python
if "breadcrumbs" in event:
    for crumb in (event["breadcrumbs"].get("values") or []):
        if "data" in crumb and isinstance(crumb["data"], dict):
            crumb["data"] = _redact_value(crumb["data"])
        if "message" in crumb and isinstance(crumb["message"], str):
            crumb["message"] = _redact_string(crumb["message"])
```

---

### F2 — MEDIUM: `_sentry_before_send` does not walk `logentry` or `request` fields

**File:** `KrabEar/backend/observability.py` — `_sentry_before_send` function
**Description:**
Sentry Python SDK may populate additional top-level fields that the current redaction loop misses:
- `logentry` — structured log message (populated when `capture_message()` is called with a dict); its `message` and `params` can contain path strings.
- `request` — HTTP request data (URL, headers, body) relevant if the REST server (`rest_server.py`) is in the same process or if future call paths use sentry's HTTP integration.

Confirmed with a live test:
```python
event = {'logentry': {'message': 'file /Users/alice/data/test.py not found'}}
result = _sentry_before_send(event, None)
# result['logentry']['message'] still contains the unredacted path
```

The current loop only covers `extra`, `contexts`, `tags`:
```python
for top_key in ("extra", "contexts", "tags"):
    if top_key in event:
        event[top_key] = _redact_value(event[top_key])
```

**Impact:** Paths in `logentry` messages and `request` bodies escape redaction.

**Fix:** Extend the `top_key` loop to include `logentry` and `request`:
```python
for top_key in ("extra", "contexts", "tags", "logentry", "request"):
    if top_key in event:
        event[top_key] = _redact_value(event[top_key])
```

---

### F3 — LOW: `privacy_mode_enabled` True→False re-enable does not re-init Sentry

**File:** `KrabEar/backend/settings_service.py` lines 371–384
**Description:**
The W1199 fix correctly handles the `privacy_mode_enabled` False→True flip (disable Sentry on enable privacy mode). However, the reverse direction (True→False, i.e. user disables privacy mode after enabling it) is not handled. When privacy mode is disabled via IPC:
- `_sentry_initialized` remains `False`
- `sentry_sdk.init()` is never re-called
- Sentry stays silenced until process restart

This means a user who toggles privacy mode off via settings UI will not see Sentry re-activate without restarting Krab Ear, contrary to the expected symmetric behaviour.

**Impact:** LOW — intentionally conservative (privacy mode disable requiring restart is defensible). But the asymmetry is not documented and may confuse operators expecting live toggle.

**Fix (opt.):** In `settings_service.handle_set_settings`, detect `privacy_mode_enabled` flipping from `True` to `False` and call `init_sentry(dsn=..., settings={"privacy_mode_enabled": False})` to re-activate telemetry. Or document the restart requirement explicitly in the settings IPC response.

---

### F4 — LOW: W1193 F3 (sentry bypass in `_handle_send_errors_to_sentry`) remains open

**File:** `KrabEar/backend/service.py` — `_handle_send_errors_to_sentry` (approx. line 1330)
**Description:**
This is the W1193 F3 MEDIUM finding that was tracked as W1202 but never merged. The handler directly imports `sentry_sdk` and calls `sentry_sdk.add_breadcrumb(data=err.context)` and `sentry_sdk.capture_message()` without checking `is_sentry_initialized()`. Two risks remain:
1. If Sentry was never initialized (no DSN), the SDK import succeeds but the resulting SDK state is undefined — could interact with any in-process Sentry from third-party deps.
2. `data=err.context` passes `KrabError.context` (arbitrary caller-supplied dict) without redaction. Current wired error contexts are metadata-safe, but there is no enforcement.

**Impact:** LOW-MEDIUM. Currently benign (existing error contexts are safe), but creates a fragile pattern that could cause path leakage if new error codes add path context.

**Fix:** Replace direct `sentry_sdk` calls with `observability.add_breadcrumb()` + `observability.capture_exception()`, guarded by `is_sentry_initialized()`.

---

### F5 — LOW: No test coverage for `breadcrumbs` / `logentry` redaction gaps (F1+F2)

**File:** `KrabEar/tests/test_sentry_before_send_W1201.py`
**Description:**
The 17 tests in `test_sentry_before_send_W1201.py` cover:
- Frame `filename`, `abs_path`, `vars` redaction
- `extra`, `tags`, `message` redaction
- Transcript path dropping

But there are **no tests** for:
- `breadcrumbs` field walking (F1 above)
- `logentry` field walking (F2 above)
- `request` field walking (F2 above)

This means F1 and F2 were not caught during the W1201 fix review. A test asserting that `breadcrumbs[*].data` is redacted would have revealed the gap.

**Impact:** LOW — test gap only; no production behaviour change. But coverage is the correct enforcement layer for privacy contracts.

**Fix:** Add test cases:
- `test_before_send_redacts_breadcrumb_data_paths`
- `test_before_send_redacts_logentry_message`

---

## Test Coverage Summary (post-W1199/W1201)

| Area | Tests | Status |
|---|---|---|
| init_sentry no-op when DSN absent | `test_observability.py` | COVERED |
| privacy_mode_enabled False→True at startup | `test_init_sentry_privacy_W1199.py` | COVERED |
| privacy_mode_enabled flip via IPC | `test_init_sentry_privacy_W1199.py` | COVERED |
| `before_send` exception/frame/vars redaction | `test_sentry_before_send_W1201.py` | COVERED |
| `before_send` extra/tags/message redaction | `test_sentry_before_send_W1201.py` | COVERED |
| `include_local_variables=False` passed | `test_sentry_before_send_W1201.py` | COVERED |
| sentry-sdk version pin `<3.0` | `requirements.txt` static | COVERED |
| `before_send` breadcrumbs redaction | NONE | **GAP** (F1, F5) |
| `before_send` logentry/request redaction | NONE | **GAP** (F2, F5) |
| privacy re-enable True→False | NONE | **GAP** (F3) |
| `_handle_send_errors_to_sentry` bypass | NONE | **GAP** (F4 / W1202) |

---

## Priority

| # | Severity | Finding | Action |
|---|---|---|---|
| F1 | MEDIUM | breadcrumbs not walked in before_send | Fix `_sentry_before_send` + add test |
| F2 | MEDIUM | logentry/request not walked in before_send | Fix `_sentry_before_send` + add test |
| F3 | LOW | privacy True→False re-enable not handled | Fix or document restart requirement |
| F4 | LOW | W1202 (sentry bypass) still open | Wire W1202 fix |
| F5 | LOW | Missing tests for F1/F2 | Add breadcrumbs + logentry test cases |
