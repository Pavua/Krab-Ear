# W1351 Audit: AuditLogger (`backend/audit_logger.py`)

**Date:** 2026-05-27  
**Auditor:** W1351 (sub-agent, read-only)  
**Branch:** `audit-audit-logger-W1351`  
**File:** `KrabEar/backend/audit_logger.py` (178 lines)

---

## Summary

5 findings. The most critical is that `AuditLogger` is **never instantiated or called at runtime** — it is a dead module. The IPC dispatch path (`handle_request`) has no call-site for `log_request`, no `_audit_logger` attribute exists on `BackendService`, and the shutdown handler that references `_audit_logger` silently no-ops via `getattr(service, "_audit_logger", None)`. All test coverage exercises the class in isolation and never catches the wiring gap.

---

## Findings

### F1 — CRITICAL: AuditLogger is not wired to IPC dispatch (dead module)

**Severity:** Critical  
**File:** `KrabEar/backend/service.py`

`AuditLogger` is never imported in `service.py`. `BackendService` has no `_audit_logger` attribute. The `handle_request` method (lines 887–1294) completes every IPC call without calling `log_request`. The `GracefulShutdownHandler._flush_audit_log()` uses `getattr(service, "_audit_logger", None)` which silently returns `None` and skips flushing.

Result: zero IPC operations are recorded to `audit_*.ndjson` at runtime, despite the module, its tests, and the shutdown handler's docstring all assuming it is active.

**Fix:** Import `AuditLogger` in `service.py`, instantiate it in `BackendService.__init__` as `self._audit_logger = AuditLogger(self._data_dir)`, and call `self._audit_logger.log_request(method, params, result, duration_ms)` at the bottom of `handle_request` (after result is computed, inside a try/except to never block the response).

---

### F2 — MEDIUM: `_SENSITIVE_METHODS` list is under-inclusive for path/PII leakage

**Severity:** Medium  
**File:** `KrabEar/backend/audit_logger.py`, lines 20–24

The `_SENSITIVE_METHODS` guard (line 70) only omits `params_keys` for `set_settings`, `set_notification_preferences`, and `apply_profile_preset`. `params_keys` is the *list of parameter key names*, not values — so it leaks schema information like `["api_key", "password", "sentry_dsn"]` for `set_settings` if that method were accidentally removed from the set.

More importantly, several methods pass path or text content as parameter *keys* in practice (e.g. if a caller were to inline a transcript into a param name, pathological but possible). The real risk is for methods like `transcribe_paths` where `params_keys` would log `["paths"]` — harmless by itself but reveals that the call occurred with path input. Methods that handle DSN or credentials (e.g. `set_call_provider`, `set_twilio_account_sid`) are not in `_SENSITIVE_METHODS`.

Currently this is moot because F1 means nothing is actually logged. Once F1 is fixed, the list needs expansion to cover all credential/key-setting methods.

**Fix:** Expand `_SENSITIVE_METHODS` to cover all methods containing secrets: any `set_*` method that sets API keys, DSN, auth tokens. A pattern-based approach (`method.startswith("set_") and any(k in params for k in ("api_key", "dsn", "auth_token", ...))`) would be more robust than a static frozenset.

---

### F3 — MEDIUM: `_cleanup_old_files` is called outside the lock — TOCTOU race

**Severity:** Medium  
**File:** `KrabEar/backend/audit_logger.py`, lines 80–91

```python
with self._lock:
    self._rotate_if_needed(today)
    # ... write line ...
self._cleanup_old_files()   # <- outside the lock
```

`_cleanup_old_files` calls `glob()` and `unlink()` without holding `self._lock`. Two concurrent threads calling `log_request` could both enter `_cleanup_old_files` simultaneously, both find N > `_KEEP_DAYS` files, and both attempt to `unlink` the same old files. The second `unlink` would raise `FileNotFoundError` (silently swallowed by the `except Exception` wrapper), but in a high-concurrency scenario both could also race against the rotation that just opened a new file, potentially unlinking today's file.

**Fix:** Either move `_cleanup_old_files` inside the lock, or use `unlink(missing_ok=True)` and ensure idempotent glob ordering. Moving inside the lock is simplest.

---

### F4 — LOW: No privacy_mode suppression — audit log records IPC calls during privacy mode

**Severity:** Low  
**File:** `KrabEar/backend/audit_logger.py` + `KrabEar/backend/service.py`

`BackendService` has `enable_privacy_mode` / `disable_privacy_mode` IPC handlers that set a runtime flag suppressing transcription logging and triggering `PrivacyAuditLogger` events. However, `AuditLogger.log_request` has no awareness of this flag. Once wired (F1 fix), it would record every IPC method name and its `params_keys` during privacy mode — revealing that `start_recording`, `stop_recording`, `translate_text`, etc. were called, even if the transcript content itself is not logged.

For a compliance-conscious audit trail this is expected behavior, but it contradicts the spirit of privacy mode and is undocumented.

**Fix:** Pass or read `privacy_mode` flag in `log_request`. During privacy mode, consider omitting `params_keys` entirely or logging only method and duration. Add a `privacy_mode` field in the audit entry when the flag is active to make the suppression explicit.

---

### F5 — LOW: No tamper detection / hash-chain (W974 pattern not applied)

**Severity:** Low  
**File:** `KrabEar/backend/audit_logger.py`

The W974 hash-chain pattern (each log entry contains `prev_hash` of the prior entry, enabling detection of deletions or modifications) is not implemented. The NDJSON files are append-only by convention but are not protected by any integrity check. An attacker with filesystem access can silently delete or modify individual lines; `get_audit_log` will read tampered content without complaint.

Given that this is a local single-user app this is a low risk, but if audit logs are intended for compliance / forensic review (e.g., confirming privacy-mode transitions), tamper evidence would add value.

**Fix (optional):** Apply the W974 pattern: each entry includes `prev_hash: sha256(prev_line_json)`. `get_audit_log` can optionally verify chain integrity and return a `chain_ok: bool` field. Alternatively, add `IntegrityChecker`-style validation for audit files.

---

### F6 — LOW: `_cleanup_old_files` counts files, not calendar days

**Severity:** Low  
**File:** `KrabEar/backend/audit_logger.py`, lines 168–177

The docstring says "deletes files older than `_KEEP_DAYS` days" but the implementation keeps the last `_KEEP_DAYS` *files by sorted filename*:

```python
if len(files) <= _KEEP_DAYS:
    return
for old_file in files[: len(files) - _KEEP_DAYS]:
    old_file.unlink()
```

If the backend is stopped for 10 days and restarted, only 7 files would exist and none would be deleted — correct. But if an external process creates additional `audit_*.ndjson` files (e.g., from a test run in the same data dir), more than 7 actual days of audit data will be retained, or files from today could be deleted if the alphabetical sort places a malicious filename early. The count-based approach is fragile vs. the stated date-based intent.

**Fix:** Parse the date from the filename (`audit_YYYY-MM-DD.ndjson`) and delete files where the date is older than `datetime.now(timezone.utc).date() - timedelta(days=_KEEP_DAYS)`.

---

## Wire Status

| Aspect | Status |
|---|---|
| `AuditLogger` imported in `service.py` | No |
| `_audit_logger` attribute on `BackendService` | No |
| `log_request` called in `handle_request` | No |
| `_flush_audit_log` in `GracefulShutdownHandler` | Referenced but no-ops (getattr → None) |
| IPC handlers `get_audit_log` / `clear_audit_log` | Not exposed in `handle_request` dispatch |
| `get_audit_log` method exists on `AuditLogger` | Yes (tested) |

## Test Coverage

`KrabEar/tests/test_audit_logger.py` — 4 test classes, ~25 test methods. Coverage is thorough for unit-level behavior (rotation, thread-safety, sensitive method redaction, persistence, edge cases). However, all tests exercise `AuditLogger` in isolation without verifying it is called from `BackendService.handle_request`. No integration test catches the wiring gap (F1).

A test like `test_handle_request_calls_audit_logger` that patches `BackendService._audit_logger` and asserts `log_request` is called would have caught F1.
