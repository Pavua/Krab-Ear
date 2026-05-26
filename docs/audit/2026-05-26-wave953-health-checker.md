# Wave 953 — HealthChecker Audit

**Date:** 2026-05-26  
**Auditor:** W953 (sub-agent)  
**Files audited:**
- `KrabEar/backend/health_checker.py` (212 lines)
- `KrabEar/backend/health_check_service.py` (209 lines)
- `KrabEar/tests/test_health_checker.py` (466 lines)

**Note:** `HealthChecker` (aggregate readiness checks) is distinct from `HealthCheckService`
(W683 extraction — IPC handler wrapper). This audit covers only `HealthChecker`.

---

## Summary

`HealthChecker` is well-structured and isolation is solid. Five findings were confirmed,
ranging from a silent false-positive for unwarm STT to a missing IPC socket probe and
zero caching on a medium-throttled method.

---

## Findings

### F1 — False Positive: unwarm STT reports "ok" (MEDIUM)

**Location:** `health_checker.py:87–97`

When `engine.current_model` is `None` (model not yet loaded post-startup), the checker
falls back to reading `settings.MODEL_BALANCED` from the config singleton and returns
`{"status": "ok", "cached": False}`.

```python
# health_checker.py:87-97
if current_model:
    return {"status": "ok", "model": current_model, "cached": cached}
else:
    # Модель известна из конфига, но не загружена — это нормально
    ...
    return {"status": "ok", "model": model_name, "cached": False}
```

The comment says "it is normal for the model not to be loaded" — but this means that
immediately after cold-start, before any recording has warmed up MLX, `health_check`
returns `status: "healthy"` (or at worst `"degraded"` due to other reasons) even though
STT would fail if triggered. A caller polling this endpoint for readiness gets a false "go"
signal. Consider returning `{"status": "warming_up"}` when `cached=False and current_model=None`
and treating that as "degraded" rather than "ok".

---

### F2 — No IPC Socket Probe (LOW)

**Location:** `health_checker.py:47–67` (no socket check present)

The task description asks whether `_check_*` verifies the Unix socket is listening via
`socket.connect`. It does not: there is no IPC socket check at all in `check_all()`.
The five subsystems checked are: `stt_model`, `llm`, `disk_space`, `history_store`,
`audio_devices`. There is no `ipc_socket` entry.

The test file acknowledges this gap explicitly at line 408–421, stating that
`history_store` and `disk_space` serve as an indirect "IPC readiness proxy". This is
technically correct (if the store is reachable so is the backend), but it does not verify
that the Unix socket itself is accepting connections — a foreign process could find the
socket file present but `listen()` not yet called, or the socket unlinked/re-created.

For a full production readiness signal, add a lightweight `_check_ipc_socket` that
calls `socket.connect(socket_path)` and immediately `socket.close()`.

---

### F3 — No Result Caching; medium-throttled but still re-runs everything on each call (LOW)

**Location:** `health_checker.py:47–67`, `ipc_throttle.py:72`

`check_all()` has **no TTL cache**. Every invocation runs all five subsystem checks
sequentially. `health_check` is classified as `MEDIUM` (30/min) in `ipc_throttle.py`,
so the rate-limiter provides a coarse outer guard, but:

- 30 calls/minute = one call every 2 seconds. `HealthMonitor.swift` pings every 3 seconds
  via `handle_ping` (not `health_check`), so the ping path is unaffected. However, any
  GUI client polling `health_check` (e.g. status panel refresh loop) can still hammer all
  five subsystems 30 times/minute.
- `sd.query_devices()` (sounddevice) is a PortAudio C call that can block for tens of
  milliseconds on some macOS configurations when audio devices are hot-plugging.
- There is no inter-call dedup or minimum interval within `HealthChecker` itself.

**Recommendation:** add a `_cache: tuple[float, dict] | None = None` with a 10-second
TTL so that burst polling doesn't re-query sounddevice on each call.

---

### F4 — Cascading Timeout: no per-check or total timeout ceiling (LOW)

**Location:** `health_checker.py:47–67`

Checks run sequentially with no `threading.Timer`, `signal.alarm`, or `concurrent.futures`
timeout wrapper. If `sd.query_devices()` or `self._store.count_active_items()` (file lock
contention) blocks, the entire `check_all()` call blocks the IPC dispatch thread for an
unbounded duration.

In practice `_check_audio_devices` is the highest-risk blocker: PortAudio device
enumeration is known to stall on macOS when a USB audio device is initializing. The
`_check_history_store` call (`count_active_items`) holds a file lock during compaction.

The existing try/except around each check catches Python-level exceptions but cannot
interrupt a C-extension or OS-level blocking call. No total ceiling is enforced.

**Recommendation:** run the five checks concurrently via `ThreadPoolExecutor(max_workers=5)`
with a 2-second per-future timeout (`future.result(timeout=2.0)`), surfacing timeouts as
`{"status": "timeout"}` per check.

---

### F5 — No Sentry Breadcrumb on Health Failures (INFO)

**Location:** `health_checker.py:99–101`, `119–120`, `142`, `158–159`, `185–186`

Each check logs failures via `logger.warning(...)`, which is correct. However, none of
the checks call `add_breadcrumb()` from `backend/observability.py`. This means health
check failures are invisible in Sentry crash reports — if a crash occurs shortly after
`stt_model` or `disk_space` started returning `"error"`, that context is not attached to
the subsequent crash event.

Compare with the IPC handler layer (e.g. `service.py` breadcrumbs from PR #238) where
every `handle_request` call emits a breadcrumb with `category="ipc"` and `data.ok`.

`HealthCheckService.handle_health_check` (the IPC layer, `health_check_service.py:99–101`)
is the natural place to add a breadcrumb after calling `self._health_checker.check_all()`,
e.g.:

```python
result = self._health_checker.check_all()
add_breadcrumb(
    category="health_check",
    message="health_check",
    data={"status": result["status"], "unhealthy_checks": [
        k for k, v in result["checks"].items() if v.get("status") not in ("ok", "unavailable")
    ]},
)
return result
```

---

## Positive Findings (no action needed)

| Topic | Verdict |
|---|---|
| **Cyclic dependency** | None. `HealthChecker` imports only stdlib, `shutil`, `pathlib`, `core.config` (lazy), and `sounddevice` (lazy). It does not import `BackendService`, `HealthCheckService`, or `StartupDiagnostics`. No cycle risk. |
| **Disk check partition** | Correct. `_check_disk_space` calls `shutil.disk_usage(data_dir)` — the actual data directory, not root. Falls back to `data_dir.parent` when dir not yet created. |
| **Thread safety** | `check_all()` reads only immutable references (`self._store`, `self._transcriber`, etc.) and delegates to each subsystem's own thread-safe methods. No shared mutable state inside `HealthChecker`. Safe for concurrent calls (verified by `test_concurrent_check_safe`). |
| **Production wire** | IPC method `health_check` → `BackendService._handle_health_check` (service.py:1009,1709) → `self._health_checker.check_all()`. Also wired in REST server (`rest_server.py:508–510`). Active handler, not dead. |
| **Test coverage** | `test_health_checker.py` — 466 lines, 9 test classes, covers: structure, all 5 subsystems, STT model states (cached/uncached/None/exception), LLM circuit states, disk (ok/warning/critical/error), history (ok/error/zero), aggregate logic (all 4 paths), full integration, concurrency. Coverage is thorough. |

---

## Findings Summary

| # | Title | Severity | Fix cost |
|---|---|---|---|
| F1 | Unwarm STT falsely reports "ok" | MEDIUM | Small (add warming_up status) |
| F2 | No IPC socket probe in check_all() | LOW | Small (add _check_ipc_socket) |
| F3 | No result caching; audio device re-queried on each call | LOW | Small (10s TTL dict cache) |
| F4 | No per-check or total timeout ceiling | LOW | Medium (ThreadPoolExecutor) |
| F5 | Health failures not emitted as Sentry breadcrumbs | INFO | Trivial (2 lines) |
