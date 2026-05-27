# Audit: observability.py — Sentry/GlitchTip integration (W1193)

**Date:** 2026-05-26  
**Branch:** audit/audio-quality-residual-W1100  
**Scope:** `KrabEar/backend/observability.py` + all call-sites  
**Auditor:** W1193 sub-agent (read-only)

---

## Summary

5 findings: 1 HIGH, 2 MEDIUM, 2 LOW.  
No raw transcript text was observed in breadcrumb data across all call-sites — the metadata-only discipline is well-maintained. However, four structural gaps exist.

---

## Findings

### F1 — HIGH: `privacy_mode_enabled` not consulted at startup `init_sentry` call

**File:** `KrabEar/backend/service.py:3837`  
**Description:**  
`init_sentry()` accepts a `settings` dict and skips init when `privacy_mode_enabled=True` (line 122 of `observability.py`). However, the only call-site in `service.py:main()` does NOT pass the `settings` parameter:

```python
sentry_ok = init_sentry(
    dsn=settings.SENTRY_DSN or None,
    environment=settings.SENTRY_ENVIRONMENT,
    release=get_release_string(),
    # settings= NOT PASSED — privacy_mode_enabled check is dead code at startup
)
```

`privacy_mode_enabled` lives only in `DEFAULT_SETTINGS` / `settings.json` runtime dict (not in the pydantic `Settings` class — it has no `PRIVACY_MODE_ENABLED` field). This means a user who sets `privacy_mode_enabled=True` via IPC will prevent future re-inits but Sentry is already initialized at process start before the settings.json runtime value is ever consulted.

**Impact:** Sentry telemetry is active for the lifetime of the process even when the user has chosen privacy mode, because init happens before the runtime settings dict is loaded.

**Fix:** Load runtime settings.json at startup before calling `init_sentry`, or add `PRIVACY_MODE_ENABLED` to the pydantic `Settings` class so it is env-overridable and available at `main()` time.

---

### F2 — MEDIUM: `sentry-sdk>=2.0` — no upper bound, major-version breakage risk

**File:** `KrabEar/requirements.txt:44`  
**Description:**  
The SDK is pinned as `sentry-sdk>=2.0` with no upper bound. Sentry SDK 3.x (expected in 2026) will break the `push_scope` context-manager API used in `capture_exception` (line 185) and the signal handlers (line 246) — both use `with sentry_sdk.push_scope() as scope:` which was deprecated in SDK 2.x and removed in SDK 3.x in favour of `sentry_sdk.new_scope()`.

Additionally, `before_send`, `before_breadcrumb`, and default integration lists changed between SDK major versions. Without an upper bound, a `pip install --upgrade` can silently break crash reporting.

**Impact:** Silent telemetry breakage on any environment that runs `pip install --upgrade`.

**Fix:** Pin as `sentry-sdk>=2.0,<3.0` until the push_scope migration is completed.

---

### F3 — MEDIUM: Direct `sentry_sdk` bypass in `service.py._handle_send_errors_to_sentry`

**File:** `KrabEar/backend/service.py:1737–1760`  
**Description:**  
The `_handle_send_errors_to_sentry` handler imports and calls `sentry_sdk` directly, bypassing the `observability` module entirely:

```python
import sentry_sdk
...
sentry_sdk.add_breadcrumb(
    category=err.component,
    message=err.code,
    level=err.severity,
    data=err.context,   # KrabError.context dict — arbitrary caller-supplied fields
)
sentry_sdk.capture_message(...)
sentry_sdk.flush(timeout=2.0)
```

Two problems:
1. The bypass means `_sentry_initialized` guard in `observability.py` is not consulted — if Sentry was never initialized (DSN absent), the import succeeds but `add_breadcrumb` calls go to a live SDK object that was initialized elsewhere in-process (e.g. from LM Studio's bundled Sentry). This is fragile.
2. `data=err.context` passes the raw `KrabError.context` dict which is populated by callers. Currently all wired error contexts contain only metadata (method names, wait times, UUIDs), but there is no enforcement preventing a future caller from including sensitive data. The `observability.add_breadcrumb` wrapper at least documents the privacy contract; the direct call does not.

**Fix:** Route this handler through `observability.add_breadcrumb()` + `observability.capture_exception()` and guard with `is_sentry_initialized()` check before importing the SDK.

---

### F4 — LOW: No `before_send` filter — local file paths leak in exception stack traces

**File:** `KrabEar/backend/observability.py:146–152`  
**Description:**  
`sentry_sdk.init()` is called without a `before_send` callback. Sentry's default behaviour for Python exceptions includes local variable values in stack frames. The backend processes audio file paths, which may expose the user's home directory structure (e.g. `~/Library/Application Support/KrabEar/transcripts/2026-05-26_recording.wav`) in Sentry issue details.

`send_default_pii=False` prevents IP addresses and cookies but does NOT strip local variable values from stack frames — those require an explicit `before_send` that scrubs path-like strings, or `include_local_variables=False` (SDK 2.x option).

**Impact:** Local filesystem paths visible in Sentry dashboard for crash reports. Home directory revealed.

**Fix:** Add `include_local_variables=False` to `sentry_sdk.init()`, or add a `before_send` callback that removes `vars` from all stack frames.

---

### F5 — LOW: `release_from_git` called even when explicit `release=` is provided to `init_sentry`

**File:** `KrabEar/backend/observability.py:141`  
**Description:**  
When `init_sentry` is called with an explicit `release` string (as in `service.py:3840` which passes `get_release_string()`), the code correctly avoids calling `release_from_git()`:

```python
resolved_release = release if release is not None else release_from_git()
```

However `get_release_string()` (the caller) itself calls `_read_version_from_plist()` which opens a file and runs `subprocess.run` via `release_from_git()` as a fallback. The result is that at least one file open + subprocess call always happens at startup regardless of whether a version is available, adding ~5 ms latency on a cold start with no git repo (common in production `.app` invocations where the working directory is not the repo root).

**Impact:** Minor latency + unnecessary subprocess at startup. Lower priority since it does not affect correctness.

**Fix:** Cache `get_release_string()` result at module level, or move the git fallback into `release_from_git()` only and short-circuit `get_release_string()` when plist is readable.

---

## Test coverage assessment

**Well covered:**
- No-op when DSN absent (`test_none_dsn_returns_false`, `test_init_sentry_with_none_dsn_is_noop`)
- `send_default_pii=False` enforced by test (`test_send_default_pii_is_false`)
- `mask_phone` masking (`TestMaskPhone`)
- Thread safety of `add_breadcrumb` (`TestConcurrentBreadcrumbsThreadSafe`)
- Privacy guard: no transcript text in breadcrumb data (`test_breadcrumb_data_contains_no_transcript_text`)
- Release priority chain (`GetReleaseStringTests`)
- Signal handler idempotency (`TestInstallSignalHandlers`)

**Gaps (untested):**
- F1: no test that `init_sentry(settings={"privacy_mode_enabled": True})` is actually called with a live settings dict at startup
- F2: no test that SDK version mismatch (push_scope removed) is detected
- F3: `_handle_send_errors_to_sentry` direct sentry_sdk path has no unit test for the privacy bypass
- F4: no test that local variable values are not emitted (requires `before_send` inspection)

---

## Breadcrumb category coverage

Categories in use: `recording`, `transcription`, `translation`, `settings`, `history`, `call`, `ipc`.  
Coverage is comprehensive. High-frequency polling methods correctly excluded via `_BREADCRUMB_EXCLUDED_METHODS` (`ping`, `get_recording_state`, `live_subs_ingest`, etc.).

No transcript text, glossary terms, or translated content was found in any `data=` argument across all 30+ call-sites inspected.

---

## PR #241 release tracking verification

`get_release_string()` priority chain (env → Info.plist → `__version__.py`) is correctly implemented and tested. `init_sentry` in `service.py:main()` passes `release=get_release_string()` explicitly, bypassing the `release_from_git()` git-describe fallback — this is correct for production bundles. The W704 fix is in place and regression-guarded by `GetReleaseStringTests.test_no_2_0_0_regression`.
