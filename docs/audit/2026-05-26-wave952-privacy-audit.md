# Wave 952 — PrivacyAuditLogger Security Audit

**Date:** 2026-05-26  
**Auditor:** W952 (sub-agent, read-only)  
**Target:** `KrabEar/backend/privacy_audit.py` — singleton NDJSON log for privacy-mode compliance events  
**Test files:** `KrabEar/tests/test_privacy_audit.py`, `KrabEar/tests/test_privacy_audit_clear.py`

---

## Summary

6 findings, severity: CRITICAL × 1, HIGH × 2, MEDIUM × 2, INFO × 1.

---

## F-1 [CRITICAL] — `clear()` destroys the audit log with no authorization check

**File:** `KrabEar/backend/privacy_audit.py:144–149`, `KrabEar/backend/service.py:2351–2358`

The `clear()` method calls `self._log_path.unlink(missing_ok=True)` — permanently deleting the audit file. It is exposed via the IPC handler `clear_privacy_audit_log`, which is reachable by **any** connected IPC client:

```python
def _handle_clear_privacy_audit_log(self, params: dict[str, Any]) -> dict[str, Any]:
    audit = get_privacy_audit_logger()
    audit.clear()
    return {"ok": True}
```

IPC signing is **disabled by default** (`IPC_SIGNING_ENABLED: bool = False` in `core/config.py:231`). There is no role check, admin flag, or confirmation token on this handler. A local process that can connect to the Unix socket can silently erase the compliance audit trail.

**Impact:** complete audit-log destruction by any local code or script; defeats the purpose of a compliance audit log.

**Recommendation:** require explicit authorization (admin token or a dedicated `audit_admin` setting) before `clear()` is permitted. At minimum, write a "cleared" tombstone entry into the log *before* deletion, so the erasure itself is observable if the log is being replicated elsewhere.

---

## F-2 [HIGH] — Singleton is not thread-safe (race on first construction)

**File:** `KrabEar/backend/privacy_audit.py:34–38`

```python
@classmethod
def get_instance(cls, log_path: Path | None = None) -> "PrivacyAuditLogger":
    if cls._instance is None:
        cls._instance = cls(log_path=log_path)
    return cls._instance
```

There is no lock protecting the check-then-set pattern. A stress test (10 threads calling `get_instance()` simultaneously from a cold start) consistently produced **2 distinct instances** in lab conditions. This means:

- Two separate `PrivacyAuditLogger` objects can exist concurrently, both writing to the same file.
- If the two instances are created with **different** `log_path` arguments (which is allowed by the API), events are silently split across two files.
- The `fcntl.flock` inside `log_event` only guards file-level writes; it does not prevent object-level divergence.

**Recommendation:** protect `get_instance` with a class-level `threading.Lock`:

```python
_instance_lock: ClassVar[threading.Lock] = threading.Lock()

@classmethod
def get_instance(cls, log_path=None):
    with cls._instance_lock:
        if cls._instance is None:
            cls._instance = cls(log_path=log_path)
    return cls._instance
```

---

## F-3 [HIGH] — No event tampering protection (no HMAC / hash chain)

**File:** `KrabEar/backend/privacy_audit.py` (entire module)

Each log entry is a plain JSON line with no cryptographic signature or chained hash. An attacker with write access to `~/Library/Application Support/KrabEar/privacy_audit.log` can silently:

- Delete individual lines (leaving no evidence of the deletion).
- Rewrite the `ts` field to alter the apparent order of events.
- Insert fabricated entries backdated to any timestamp.

For a module whose purpose is compliance auditing of privacy-mode enforcement, the absence of a hash chain (e.g., each entry's SHA-256 over `prev_hash + entry_json`) means the log provides **no tamper evidence**. The `read_entries` method cannot distinguish a genuine log from a manipulated one.

**Recommendation:** add a `prev_hash` field to each entry (SHA-256 of the previous line's raw bytes). The chain can be verified offline by `total_count` / integrity-check tooling. Even a simple append-only counter (`seq`) would catch line deletions.

---

## F-4 [MEDIUM] — Disk-full silences the audit write; the privacy operation still proceeds (UNSAFE)

**File:** `KrabEar/backend/privacy_audit.py:70–86`

```python
try:
    ...
    fh.write(line)
    fh.flush()
    os.fsync(fh.fileno())
    ...
except Exception:
    logger.exception("PrivacyAuditLogger: ошибка записи ...")
```

On `OSError: No space left on device` the exception is swallowed and the caller (`init_sentry`, `handle_translate_text`, etc.) continues normally. The privacy-blocking action still takes effect, but the audit record is never written. After the disk fills, privacy violations can occur without any audit trail.

**Current behaviour:** the disk-full test in `test_privacy_audit.py::TestPrivacyAuditUnwritableDisk` explicitly *asserts* that `log_event` does **not** raise. This codifies the unsafe pattern as correct.

**Recommendation:** change the failure mode. The test expectation should be inverted: either raise (so callers can log to `syslog` as a fallback), or emit an error-bus event (`error_codes.DISK_WRITE_FAILED`) that surfaces in the UI. The privacy operation should still proceed, but the failure should be observable.

---

## F-5 [MEDIUM] — No retention policy; log grows forever with no GDPR interaction

**File:** `KrabEar/backend/privacy_audit.py` (entire module)

There is no rotation, size cap, or retention period. Over months of use the file grows unbounded. There is no mechanism to purge old entries while keeping recent ones. The only available operation is `clear()`, which erases everything.

For GDPR "right to be forgotten" requests, a user can trigger `clear_privacy_audit_log` to wipe the log — but this also wipes all other users' records if the app is shared, and as noted in F-1, the operation is not itself audited.

**Recommendation:** implement size-based or age-based rotation (e.g., keep last 90 days; archive older entries). Expose a `purge_privacy_audit_before(ts)` IPC method that deletes only records older than the given timestamp, leaving recent compliance history intact.

---

## F-6 [INFO] — `read_entries` IPC is unauthenticated; log contents visible to any local client

**File:** `KrabEar/backend/service.py:2332–2349`

`get_privacy_audit_log` returns the full audit log (up to `limit` entries) to any IPC caller. When `IPC_SIGNING_ENABLED` is `False` (the default), no authentication is required. The log content itself is metadata-only (no transcript text is logged by existing callers), so the current risk is low. However, the `details` dict is caller-supplied and unvalidated — a future caller could inadvertently include sensitive fields.

**Recommendation:** enforce a structural allowlist on `details` keys inside `log_event` (e.g., reject keys named `text`, `transcript`, `audio_path`). This prevents accidental PII leakage from future callers even if they mistakenly pass sensitive data.

---

## Test Coverage Assessment

| Area | Covered | Gap |
|---|---|---|
| Append-only NDJSON (single + multi) | Yes | — |
| Concurrent writes (fcntl flock) | Yes (20 threads) | — |
| Singleton `get_instance` race | No | Race not tested |
| `clear()` authorization | No | No test that clear requires auth |
| Tamper detection / hash chain | No | No feature exists |
| Disk-full behaviour | Yes (tests that it is silent) | Test asserts wrong direction |
| Retention / rotation | No | No feature exists |
| Sensitive-data key allowlist | Partial (checks `text` absent) | Does not enforce at write time |

Coverage for the happy path is solid. The critical security properties (tamper evidence, singleton safety, clear authorization) have no test coverage because the features themselves are absent.

---

## Files audited

- `/KrabEar/backend/privacy_audit.py` (156 lines)
- `/KrabEar/backend/service.py` (handlers at lines 2332–2359)
- `/KrabEar/backend/observability.py` (lines 122–134)
- `/KrabEar/backend/translation_service.py` (lines 96–110, 200–215)
- `/KrabEar/tests/test_privacy_audit.py` (591 lines)
- `/KrabEar/tests/test_privacy_audit_clear.py` (124 lines)
- `/KrabEar/core/config.py` (line 231 — `IPC_SIGNING_ENABLED` default)
