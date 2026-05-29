# Audit W1615 — `backend/startup_diagnostics.py` (first pass)

**Date:** 2026-05-27  
**Auditor:** W1615 (sub-agent, read-only)  
**File:** `KrabEar/backend/startup_diagnostics.py` — `StartupDiagnostics` class (665 LOC)  
**Status:** 5 findings (1 HIGH / 2 MED / 2 LOW)

---

## Summary

`StartupDiagnostics` runs 10 independent readiness checks at backend startup and exposes results
via the `get_startup_diagnostics` IPC handler (wired through `HealthCheckService`). The module is
well-structured: all checks are non-blocking, isolated by `try/except`, and return a uniform
`CheckResult(name, status, message, duration_ms, details)` schema suitable for downstream UI.
Test coverage is strong (57 test methods in `test_startup_diagnostics.py` + 9 in
`test_startup_compact_defer_W1309.py`). Two gaps stand out: the `_error_bus` late-injection is
never performed (Sentry-level error bus toasts for cache misses are silently suppressed), and the
`_check_lm_studio_reachable` check can block startup for up to 2 seconds on every cold boot when
LM Studio is not running.

---

## Findings

### F1 HIGH — `_error_bus` never injected; `startup.stt_model_cache_miss` toasts silently dropped

**Location:** `startup_diagnostics.py:97-99`, `service.py:556-578`

`StartupDiagnostics.__init__` declares `self._error_bus: Any | None = None` with the comment
*"Late-injected by BackendService after construction (Wave 490)"*. However, `service.py` constructs
`StartupDiagnostics` at line 557 and **never** subsequently assigns `._error_bus`. The `ErrorBus`
instance exists at `self._error_bus` (line 276 in `service.py`) and is wired into the LLM rewriter,
transcriber, and `mlx_subprocess` — but not into `StartupDiagnostics`. As a result,
`_push_stt_cache_miss_error` (lines 634-664) always takes the early-exit path at line 641-642
(`if error_bus is None: return`), so the `startup.stt_model_cache_miss` user-facing toast is
**never shown** on first-install or after HF cache clear, despite being listed in `ERROR_REGISTRY`.

**Fix:** Add one line after construction in `service.py`:
```python
self._startup_diagnostics._error_bus = self._error_bus
```

---

### F2 MED — `_check_lm_studio_reachable` adds up to 2 s synchronous blocking on every cold startup

**Location:** `startup_diagnostics.py:511`

`socket.create_connection((host, port), timeout=2.0)` runs synchronously inside `run_all_checks()`,
which is called at `BackendService.__init__` (service.py:562). When LM Studio is not running (the
common case: offline, weekend, headless CI), the TCP connection attempt hangs for the full 2-second
timeout before returning `ConnectionRefusedError`. With 10 checks total, a worst-case cold startup
already has the 0.5 s socket-path check timeout on top — these are stacked synchronously on the
main service init thread, directly delaying IPC socket readiness for clients.

The `socket_path` check (line 331) has the same issue: `settimeout(0.5)` blocks for 500 ms if a
stale socket file exists and the OS does not immediately refuse.

**Fix options:** (a) cap LM Studio timeout to 0.5 s or less; (b) run the check in a `ThreadPoolExecutor`
alongside other network checks; (c) skip the check entirely at init time and only run it on
explicit IPC `get_startup_diagnostics` calls (the result is cached anyway).

---

### F3 MED — `_check_data_dir_writable` FileNotFoundError retry creates a `.startup_write_test` file and then silently swallows `OSError` variants other than `PermissionError`

**Location:** `startup_diagnostics.py:270-311`

The docstring states *"StateStore already created the directory"* making `mkdir` redundant, yet the
`FileNotFoundError` retry path (lines 284-296) calls `_try_write()` a second time without first
creating the directory. On macOS this means the retry will also raise `FileNotFoundError` (not
caught by the inner `except Exception as exc2`), which is fine — it becomes an `"error"` result.
However, the **outer** `except Exception as exc` at line 305 catches every remaining exception
including `OSError: [Errno 28] No space left on device`, which maps the check to `"error"` with
a generic message rather than the disk-specific message the user needs. The disk check at
`_check_disk_space` runs independently and would fire separately, but the overlap can produce two
confusing simultaneous `"error"` entries for the same root cause.

The `test_nonexistent_dir_created_and_ok` test (line 183) passes a genuinely nonexistent path and
expects `"ok"` — but on the initial write the directory does not exist so `FileNotFoundError` fires,
the 50 ms sleep runs, and the second attempt also fails with `FileNotFoundError` (not caught by the
inner except). This test passes only because macOS `write_text` on a missing parent raises
`FileNotFoundError` which IS caught by `except Exception as exc2` and returns `"error"` — but the
test asserts `"ok"`. This is a latent test inconsistency if behavior changes.

---

### F4 LOW — Privacy-mode interaction: none

**Location:** entire file

`StartupDiagnostics` has no awareness of `privacy_mode_enabled`. When privacy mode is active the
backend deliberately disables Sentry (`init_sentry(None)`) and stops emitting breadcrumbs (see
`service.py:229-245`). However, `run_all_checks()` unconditionally calls `add_breadcrumb(...)` at
line 158 regardless of privacy mode. The `add_breadcrumb` helper is a no-op when Sentry SDK is not
initialized (the DSN is not set), so in practice this leaks nothing — but if Sentry is configured
and the user re-enables privacy mode then toggles it off, the first call to
`get_startup_diagnostics` will emit a breadcrumb that reveals the startup health status to Sentry
even before the re-init hook has fully fired. Low risk given the no-op default, but inconsistent
with the privacy isolation contract established by W1593.

**Fix:** Guard `add_breadcrumb` behind a settings check, e.g.:
```python
if not self._settings.get("privacy_mode_enabled", False):
    add_breadcrumb(...)
```

---

### F5 LOW — `_check_socket_path_available` has zero direct unit tests; integration tests mock it away

**Location:** `KrabEar/tests/test_startup_diagnostics.py:439,467,495,698,717,738,762`

All `run_all_checks` integration test setups patch `_check_socket_path_available` out with a fixed
`"ok"` result. There are no unit test classes exercising the two real branches: (a) stale socket
file (exists, `ConnectionRefusedError`) → returns `"ok"` with `stale=True`; (b) live socket file
(connects successfully) → returns `"warning"`. The method is 46 lines of socket probing logic with
a non-trivial fallback path that warrants direct coverage.

---

## Coverage summary

| Check | Direct unit tests | Integration tests |
|---|---|---|
| `python_version` | Yes (4 tests) | Mocked in suite |
| `required_packages` | Yes (3 tests) | Mocked in suite |
| `data_dir_writable` | Yes (3 tests, 1 inconsistent) | Mocked in suite |
| `socket_path` | **No** | Mocked away |
| `ffmpeg` | Yes (3 tests) | Mocked in suite |
| `hf_token` | Yes (2 tests) | Mocked in suite |
| `stt_model_cached` | Yes (2 tests) | Mocked in suite |
| `lm_studio` | Yes (3 tests) | Mocked in suite |
| `disk_space` | Yes (4 tests) | Mocked in suite |
| `audio_devices` | Yes (3 tests) | Mocked in suite |

---

## Wire status

- `StartupDiagnostics` is constructed in `BackendService.__init__` (service.py:556-558) and
  `run_all_checks()` is called immediately (line 562) — blocking startup.
- Result is exposed via `HealthCheckService.handle_get_startup_diagnostics` → IPC
  `get_startup_diagnostics` (dispatch table entry confirmed in `ipc_dispatch.py:260`).
- `_error_bus` is NOT wired (F1 above).
- The 60 s cache TTL means `get_startup_diagnostics` IPC calls are cheap after the first one.

## Result schema

`StartupReport.to_dict()` returns a well-formed JSON-compatible dict with `status`, `version`,
`startup_time_ms`, `warnings[]`, `errors[]`, and `checks[]` (each with `name/status/message/
duration_ms/details`). This schema is suitable for the Swift diagnostics panel and SSE consumers
without changes.

## Idempotency

`run_all_checks()` is safe to call multiple times; results are cached for `cache_ttl_sec` (default
60 s). `force=True` bypasses the cache. `invalidate_cache()` resets it explicitly. No file system
side-effects survive between calls (the write test file is unlinked via `missing_ok=True`).
