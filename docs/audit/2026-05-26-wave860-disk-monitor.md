# Wave 860 Audit: DiskSpaceMonitor (`backend/disk_monitor.py`)

**Date:** 2026-05-26
**Auditor:** Wave 860 sub-agent
**File audited:** `KrabEar/backend/disk_monitor.py` (379 LOC)
**Related files:** `KrabEar/backend/error_codes.py`, `KrabEar/backend/service.py`, `KrabEar/tests/test_disk_monitor.py`, `KrabEar/tests/test_error_bus_phase_b_wave82.py`

---

## Summary

7 findings across 4 categories: 1 critical (test failure), 2 medium, 3 low, 1 info.

| # | Severity | Category | Title |
|---|----------|----------|-------|
| F1 | CRITICAL | error_bus | `disk.critical` missing from `ERROR_REGISTRY` — tests fail |
| F2 | MEDIUM | error_bus | `_disk_monitor._error_bus` never injected in `service.py` |
| F3 | MEDIUM | threshold logic | `_collect_status` level = "ok" when `shutil.disk_usage` raises — silences real disk errors |
| F4 | LOW | shutdown | `stop()` does not join with sufficient timeout visibility; no log on timeout expiry |
| F5 | LOW | edge case | `_dir_size_mb` traverses entire data_dir recursively every check interval — O(N files) cost |
| F6 | LOW | edge case | Read-only filesystem: `shutil.disk_usage` succeeds but `free_gb == 0` misclassified as "critical" even when disk is healthy external media |
| F7 | INFO | design | `check_now(force=True)` bypasses dedup — repeated IPC calls from GUI can spam the error bus |

---

## F1 — CRITICAL: `disk.critical` missing from `ERROR_REGISTRY`

**File:** `KrabEar/backend/error_codes.py`
**Status:** Bug causing test failures

`_push_disk_critical_error()` (line 307) calls:
```python
entry = ERROR_REGISTRY.get("disk.critical", {})
```

The `ERROR_REGISTRY` dict in `error_codes.py` only has `"disk.low_space"` (line 344). There is no `"disk.critical"` entry. As a result:
- `entry` is always `{}`, so `user_msg_ru` falls back to the hardcoded emoji string in the method body — acceptable at runtime, but incorrect.
- `actionable` falls back to `False` — the user never sees the "Открыть папку логов" action button on the critical toast.
- `action_id` falls back to `None`.
- `dedupe_seconds` is absent, so `ErrorBus` uses the global default (30 s) instead of the intended 600 s.

**Test impact:** `test_error_bus_phase_b_wave82.py::DiskCriticalTests::test_code_in_registry` asserts `assertIn("disk.critical", ERROR_REGISTRY)` and will fail immediately. CI is broken for this test.

**Fix:** Add the entry to `ERROR_REGISTRY` (the test at line 56-60 specifies the expected shape):
```python
"disk.critical": {
    "user_msg_ru": "🔴 КРИТИЧНО: меньше 1 GB на диске — немедленно освободите место.",
    "actionable": True,
    "action_id": "open_logs",
    "action_label": "Открыть папку логов",
    "severity": "critical",
    "dedupe_seconds": 600,
},
```

---

## F2 — MEDIUM: `_disk_monitor._error_bus` never injected in `service.py`

**File:** `KrabEar/backend/service.py` lines 616–621
**Status:** Silent production gap — disk errors never reach error bus / Sentry

`BackendService.__init__` wires `_error_bus` into `_llm_rewriter`, `transcriber`, and `_mlx_sub` (lines 283–292), but never into `_disk_monitor`:

```python
# Lines 616–621 — error_bus injection is absent:
self._disk_monitor = DiskSpaceMonitor(
    settings=settings,
    event_bus=event_bus,
    data_dir=self.store.data_dir,
)
self._disk_monitor.start()
```

`DiskSpaceMonitor._push_disk_error()` and `_push_disk_critical_error()` both guard `if error_bus is None: return`, so they silently no-op. Disk warnings only reach the EventBus (as `disk.warning` / `disk.critical` events); they never reach the error bus, never show the toast, and never go to Sentry.

The docstring on `_error_bus` (line 62) says it is "Late-injected by BackendService after construction (Phase B Wave 60)" — but the injection never happened.

**Fix:** Add one line after `self._disk_monitor.start()`:
```python
self._disk_monitor._error_bus = self._error_bus
```

---

## F3 — MEDIUM: Level silently "ok" when `shutil.disk_usage` raises

**File:** `KrabEar/backend/disk_monitor.py` lines 157–163

```python
try:
    usage = shutil.disk_usage(self._data_dir)
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
except Exception:
    free_gb = -1.0
    total_gb = -1.0
```

When `disk_usage` fails (e.g. NFS timeout, unmounted volume, permission error on the data directory), `free_gb` is set to -1.0. The level logic then guards `free_gb >= 0` before any threshold comparison, so the level becomes "ok" (the final else branch, line 179).

Returning "ok" when we actually don't know is misleading — it suppresses the warning and the `_push_disk_error` call. The test `test_handles_disk_stat_exception_gracefully` explicitly asserts `level == "ok"` (line 320), cementing this behaviour, but this assertion validates the wrong outcome.

A better sentinel would be `level = "unknown"` or at minimum a `logger.error()` call (the current `except Exception:` swallows the exception silently without even logging it — the exception handler has no body besides setting the variables).

**Note:** The exception body is bare (`free_gb = -1.0`) with no `logger.exception(...)` call, so disk-stat failures are fully invisible in logs.

**Recommended fix:**
```python
except Exception:
    logger.exception("DiskSpaceMonitor: не удалось получить статистику диска")
    free_gb = -1.0
    total_gb = -1.0
```
And update the level logic to emit a `disk.stat_failed` event or at least not return "ok".

---

## F4 — LOW: `stop()` join timeout not logged

**File:** `KrabEar/backend/disk_monitor.py` lines 96–103

```python
def stop(self) -> None:
    self._stop_event.set()
    with self._lock:
        thread = self._thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
    logger.debug("DiskSpaceMonitor остановлен")
```

If the background thread does not terminate within 5 seconds (e.g. it is blocked in a slow `rglob` over a network filesystem), `join()` returns silently and the `logger.debug` line fires as if everything is fine. The thread is now a zombie daemon — it will keep emitting events after the service considers itself stopped.

**Fix:** Check `thread.is_alive()` after `join()` and log a warning:
```python
if thread is not None and thread.is_alive():
    thread.join(timeout=5.0)
    if thread.is_alive():
        logger.warning("DiskSpaceMonitor: поток не завершился за 5 с")
```

---

## F5 — LOW: `_dir_size_mb` is O(N files) on every check interval

**File:** `KrabEar/backend/disk_monitor.py` lines 364–378

`_collect_status()` calls `_dir_size_mb(self._data_dir)` which does a full recursive `rglob("*")` over the entire data directory on every check (default: every 30 minutes). On a busy instance with thousands of transcript `.md` files this can take hundreds of milliseconds and creates many `stat()` syscalls.

The immediate symptom is latency; the secondary concern is that `_dir_size_mb` is called from the monitor thread (OK) but also from `check_now()` which is called from the IPC handler thread, potentially causing a perceptible IPC delay.

**Mitigation options:**
1. Move `_dir_size_mb(self._data_dir)` to background thread only; `check_now()` returns cached value for `data_dir_mb`.
2. Sample `data_dir_mb` less frequently than `free_space_gb` (different interval).
3. Cap recursion depth to top-level subdirectories for the data_dir total.

---

## F6 — LOW: Read-only / externally-mounted filesystem misclassification

**File:** `KrabEar/backend/disk_monitor.py` line 159

`shutil.disk_usage(self._data_dir)` reports stats for the filesystem that contains `data_dir`. On a full read-only snapshot or a mounted DMG, `usage.free` is legitimately 0. The current logic:

```python
if free_gb >= 0 and free_gb < self._settings.DISK_CRITICAL_GB:
    level = "critical"
```

…will fire `disk.critical` and push a `disk.critical` error bus entry for a filesystem state that is by design, not a failure. This could produce spurious critical toasts when running from a read-only distribution DMG or when the user's home directory is on an APFS read-only snapshot.

No immediate fix is needed for the typical macOS install (user data is on a writable volume), but the edge case is worth noting for DMG testing.

---

## F7 — INFO: `check_now(force=True)` bypasses event dedup

**File:** `KrabEar/backend/disk_monitor.py` line 129

```python
def check_now(self) -> dict[str, Any]:
    status = self._collect_status()
    self._evaluate_and_emit(status, force=True)
    return status
```

`force=True` bypasses `_last_disk_level` dedup, so every IPC call to `get_disk_status` that triggers `check_now()` while the disk is in a warning/critical state will push a new `disk.low_space` error to the error bus. If the Diagnostics panel polls this handler (e.g. every second during an open session), this can flood the error bus ring buffer.

The `ErrorBus` has its own `dedupe_seconds` mechanism (300 s for `disk.low_space`, 600 s for `disk.critical`), so this is partially mitigated — but the dedup window must be set correctly (F1 above shows it may not be for `disk.critical`).

**Consideration:** `check_now()` is documented as "for IPC use" — the force=True is intentional to show current state. Adding a cooldown to `check_now()` itself (e.g. `_last_force_check_ts`) would prevent rapid IPC polling from overloading the error bus dedup.

---

## Test coverage gaps

- No test verifies that `_error_bus` receives errors when wired (F2) — existing tests set `_error_bus = None` explicitly or use `bus` mocks but never test the `service.py` injection path.
- No test exercises the `shutil.disk_usage` silent-failure path for logging (F3 — the test `test_handles_disk_stat_exception_gracefully` exists but only checks the return value, not whether the exception is logged).
- No test covers the `stop()` timeout scenario (F4).

---

## Priority action items

| Priority | Action | Owner |
|----------|--------|-------|
| P0 (blocker) | Add `"disk.critical"` to `ERROR_REGISTRY` in `error_codes.py` | next wave |
| P1 | Wire `self._disk_monitor._error_bus = self._error_bus` in `service.py` | next wave |
| P2 | Log `shutil.disk_usage` exceptions in `_collect_status` | backlog |
| P3 | Log warning if `stop()` join times out | backlog |
| P4 | Evaluate `_dir_size_mb` caching for large vaults | backlog |
