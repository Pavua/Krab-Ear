# Observability audit — W1599

**File:** `KrabEar/backend/observability.py`
**Date:** 2026-05-27
**Wave:** W1599
**Prior fix waves:** W1193, W1199, W1201, W1202, W1483, W1490, W701/W704

## Summary

Post-W1490 the file is in good shape overall: `before_send` walks all major PII
vectors, the sentry-sdk version is correctly pinned (`>=2.0,<3.0`), thread
safety of `add_breadcrumb` / `capture_exception` is delegated to the SDK (no
shared mutable state beyond the boolean flag), and privacy-mode blocking is
correctly tested.  Five residual issues were found, two of them moderate risk.

---

## F1 — HIGH: `_sentry_initialized` not cleared when privacy mode is toggled ON at runtime

**Location:** `init_sentry()` lines 247–259; `capture_exception()` line 306; `add_breadcrumb()` line 410.

**Root cause:** All W1199 tests cover only the case where Sentry was *never*
initialised and the caller passes `privacy_mode_enabled=True`.  The runtime
scenario — "user was already running with a DSN, then enables privacy mode" —
is not guarded.  Once `_sentry_initialized = True` is set, neither
`capture_exception()` nor `add_breadcrumb()` consult the settings dict; they
only check the boolean flag.  Calling `init_sentry(dsn, settings={"privacy_mode_enabled": True})`
a second time returns `False` but **does not reset `_sentry_initialized` to
`False`**, so crash reports and breadcrumbs continue to flow to Sentry.

**Evidence:**
```python
# init_sentry (line 247) — early-exit path for privacy mode:
if settings and settings.get("privacy_mode_enabled"):
    logger.info("Sentry init skipped — privacy_mode_enabled=True")
    ...
    return False          # _sentry_initialized is NOT touched here

# capture_exception (line 306):
if not _sentry_initialized:   # True from prior call → skips the guard
    return
```

**Fix:** Add `global _sentry_initialized; _sentry_initialized = False` inside the
privacy-mode early-exit block, and add a test:
```python
def test_privacy_mode_after_init_stops_captures():
    mod._sentry_initialized = True   # simulate prior successful init
    mod.init_sentry("dsn", settings={"privacy_mode_enabled": True})
    assert not mod.is_sentry_initialized()
```

---

## F2 — MEDIUM: Transcript-path regex uses `[^/\s]+` for username, rejecting spaces

**Location:** `_redact_string()` line 49.

**Root cause:** Rule 1 (transcript path redaction) uses `[^/\s]+` as the
username character class, which rejects usernames containing a space.  macOS
short usernames cannot contain spaces, but the `Full Name` (displayed name)
*can*, and some legacy/corporate setups can have a space in the short name.
If such a path reaches the redactor it falls through to Rule 2, which uses
`[^/"']+` (spaces allowed) — but Rule 2 only collapses to `~/…`, it does NOT
drop the full transcript path, so the filename may still appear in Sentry.

The inconsistency also means the two rules have different security guarantees
depending on the username format.

**Evidence:**
```python
# Line 49 — Rule 1, username char class:
r"/Users/[^/\s]+/[^\"']*KrabEar/transcripts/[^\"']*"
#        ^^^^^^  NO spaces → fails for /Users/pa blo/...

# _HOME_PATH_RE line 25 — Rule 2, username char class:
r"/Users/[^/\"']+(/[^\"']*)"
#        ^^^^^^^  spaces OK
```

**Reproduce:**
```python
path = "/Users/pa blo/Library/Application Support/KrabEar/transcripts/2026.md"
assert _redact_string(path) == "<transcript-path-redacted>"  # FAILS
```

**Fix:** Change Rule 1 username class to `[^/"]+` (same as Rule 2):
```python
r"/Users/[^/\"']+/[^\"']*KrabEar/transcripts/[^\"']*"
```

---

## F3 — LOW: Whitespace-only DSN bypasses the `if not dsn:` guard

**Location:** `init_sentry()` line 262.

**Root cause:** `if not dsn:` does not catch a whitespace-only string like
`"   "`.  Python treats it as truthy, so a whitespace DSN is passed to
`sentry_sdk.init()`, which raises `ValueError("Invalid DSN")`.  The exception
is swallowed by the broad `except Exception` on line 291, `init_sentry` returns
`False`, and no warning is logged.  The user sees Sentry silently disabled with
no diagnostic.

**Evidence:**
```python
dsn = "   "
bool(dsn)       # True  → not caught
not dsn         # False → guard does not fire
# sentry_sdk.init("   ") raises ValueError → swallowed → returns False, no log
```

**Fix:**
```python
if not (dsn or "").strip():
    logger.debug("Sentry: DSN не задан — telemetry отключена")
    return False
```

---

## F4 — LOW: Bare `/Users/<username>` (no sub-path) not redacted by `_HOME_PATH_RE`

**Location:** `_redact_string()` line 60; `_HOME_PATH_RE` line 25.

**Root cause:** `_HOME_PATH_RE` requires at least one character after the
username separator (`/[^"']*`), so a bare `/Users/pablito` (e.g. in a
`PermissionError` message) is not matched and the username is sent to Sentry.

**Evidence:**
```python
_HOME_PATH_RE = re.compile(r"/Users/[^/\"']+(/[^\"']*)")
result = _HOME_PATH_RE.sub(r"~\1", "PermissionError at /Users/pablito")
# → "PermissionError at /Users/pablito"   ← username still present
```

**Fix:** Make the trailing sub-path group optional:
```python
_HOME_PATH_RE = re.compile(r"/Users/[^/\"']+(/[^\"']*)?")
# Replacement: lambda m: "~" + (m.group(1) or "")
```
Or use a simpler replacement: `re.sub(r"/Users/[^/\"' ]+", "~", value)` as a
post-pass when no group is needed.

---

## F5 — LOW: Missing test for `init_sentry` no-op when `sentry_sdk` not installed

**Location:** test coverage gap in all test files.

**Root cause:** `init_sentry` has an `except ImportError` branch (line 288)
that returns `False` when `sentry_sdk` is absent.  All existing tests inject a
stub module via `patch.dict(sys.modules, {"sentry_sdk": ...})`, but none tests
the code path where `sentry_sdk` is genuinely absent (not in `sys.modules` at
all).  The `test_init_sentry_handles_sdk_import_error_gracefully` in
`test_observability_coverage.py` simulates ImportError via `side_effect` on
`stub.init`, not via a missing module — that path hits the `except Exception`
branch (line 291), not the `except ImportError` branch (line 288).

**Reproduce (conceptual):**
```python
import sys
sys.modules.pop("sentry_sdk", None)
result = mod.init_sentry("https://key@sentry.io/1")
assert result is False  # not tested
```

**Severity:** LOW — the production runtime always has `sentry_sdk` installed
(pinned in `requirements.txt`), but the branch is effectively untested.

---

## Not-findings (confirmed clean)

- `sentry-sdk` version pin: `>=2.0,<3.0` — correct upper bound in `requirements.txt`.
- `before_send` coverage: walks exception frames, `extra/contexts/tags`, `message`,
  `breadcrumbs[].data` + `message`, `logentry.message` + `params`,
  `request.data/query_string/cookies` — all major PII vectors covered (W1483 F1+F2).
- `send_default_pii=False` and `include_local_variables=False` both present.
- Thread safety: no shared mutable state modified under concurrent calls;
  `add_breadcrumb` and `capture_exception` are read-only with respect to the
  `_sentry_initialized` flag and delegate mutable state to the SDK.
- `release` priority chain: `service.py` calls `get_release_string()` (env → plist → `__version__`)
  explicitly, and `init_sentry`'s internal fallback via `release_from_git()` is
  only invoked when the caller passes `release=None` (not the production call-site).
- Privacy audit log written when DSN is present and privacy mode blocks init.

---

## Test coverage gaps (summary)

| Gap | Severity |
|-----|----------|
| Already-initialized + privacy mode re-enable | HIGH (F1) |
| Transcript path redaction with space in username | MEDIUM (F2) |
| Whitespace-only DSN | LOW (F3) |
| Bare `/Users/name` (no sub-path) leak | LOW (F4) |
| Missing-module ImportError path in `init_sentry` | LOW (F5) |
