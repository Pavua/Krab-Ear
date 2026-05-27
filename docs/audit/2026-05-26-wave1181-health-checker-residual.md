# Wave 1181 — HealthChecker Residual Audit (post-W963/W953)

**Date:** 2026-05-26
**Auditor:** W1181 (sub-agent, read-only re-audit)
**Base branch:** `codex/krab-ear-v2` (HEAD: `6c900317` — v2.0.5 release)
**Prior art:**
- W953 audit (`docs/audit-health-checker-W953` branch): 5 findings F1–F5
- W963 fix (`fix/health-checker-warming-W963` branch, PR #882): F1 fix — warming_up status

---

## W963 Merge State

**NOT MERGED into `codex/krab-ear-v2`.**

PR #882 (`fix/health-checker-warming-W963`) is **OPEN**. Commit `61d94649` is reachable only
from `fix/health-checker-warming-W963`, not from `codex/krab-ear-v2`.

This means the current production code on `codex/krab-ear-v2` still contains the W953 F1 bug:
`_check_stt_model()` returns `{"status": "ok"}` when `current_model is None` and `_whisper_model
is None` (cold-start state). The warming_up fix exists only on the unmerged feature branch.

---

## W963 Fix Correctness (branch analysis)

The W963 branch fix is analysed here since its PR is open and callers need to understand whether
the fix is complete before merging.

### warming_up → ok transition mechanism

The W963 fix adds a `warming_up` status when `current_model is None and not cached`. This is
correct as a first step. However, **there is no mechanism that automatically transitions
`warming_up` → `ok`** in the health check itself. The transition happens implicitly when:

1. The first recording completes (sets `engine.current_model`), OR
2. The engine warms up via the startup warmup path.

The health check is **passive**: it reads the current state of `engine.current_model` and
`engine._whisper_model` each time it is called. Once MLX loads the model,
`engine.current_model` will be non-None and subsequent calls will correctly return `"ok"`.
There is no stuck-in-warming_up bug — the transition is natural and correct.

### _aggregate_status on warming_up

The W963 branch maps `warming_up` → `"degraded"` (added to the `warning/circuit_open/error/critical/warming_up`
set in `_aggregate_status`). This is appropriate — `warming_up` is not a fatal error and
should not trigger `"unhealthy"`.

---

## New Residual Findings (codex/krab-ear-v2 state)

### F1 — W963 Fix Not Merged: False-Positive "ok" on Cold Start Persists in Production (HIGH)

**Location:** `KrabEar/backend/health_checker.py:87–97` (codex/krab-ear-v2)
**Status:** OPEN — PR #882 unmerged

The production code in `codex/krab-ear-v2` still contains the original W953 F1 bug. On cold
start, when `engine.current_model is None` and `engine._whisper_model is None`, the checker
falls through to:

```python
# health_checker.py:87-97 on codex/krab-ear-v2 (unpatched)
else:
    # Модель известна из конфига, но не загружена — это нормально
    ...
    return {"status": "ok", "model": model_name, "cached": False}
```

A caller polling `health_check` in the first 30–60 seconds post-startup gets `status: "healthy"`
even though STT would fail immediately if triggered. The HealthMonitor.swift pings via `handle_ping`
(not `health_check`), so the supervisor is unaffected — but any GUI panel, startup diagnostic
script, or external readiness probe using `health_check` receives a false-green signal.

**Fix:** merge PR #882.

---

### F2 — _aggregate_status: "warming_up" Not in Non-Critical Error Set on codex/krab-ear-v2 (MED)

**Location:** `KrabEar/backend/health_checker.py`, `_aggregate_status` method (codex/krab-ear-v2)
**Status:** Consequence of F1 — warming_up never returned, so the set is correct for current code

On the W963 branch, `_aggregate_status` correctly adds `"warming_up"` to the degraded trigger
set. On `codex/krab-ear-v2`, `warming_up` is never returned (because F1 is present), so
`_aggregate_status` has no `"warming_up"` entry and would silently classify a `warming_up`
check as `"healthy"` if the fix were partially applied (e.g., `_check_stt_model` changed but
`_aggregate_status` not updated).

The W963 branch addresses this atomically (both `_check_stt_model` and `_aggregate_status`
updated together), so this residual is a merge-order safety concern rather than a separate code
defect. It becomes relevant if anyone cherry-picks only half of PR #882.

**Fix:** merge PR #882 atomically (no cherry-pick splitting).

---

### F3 — HealthCheckService Layer Not Exercised: service.py Bypasses HealthCheckService (MED)

**Location:** `KrabEar/backend/service.py:1708–1710` vs `KrabEar/backend/health_check_service.py`
**Status:** OPEN (present on codex/krab-ear-v2)

`HealthCheckService` was extracted (Wave 423, PR #606) and contains `handle_health_check`,
`handle_ping`, `handle_get_diagnostics`, `handle_probe_llm_http`, `handle_get_startup_diagnostics`,
and `handle_check_integrity`. However, `BackendService` on `codex/krab-ear-v2` does NOT delegate
to `HealthCheckService` — it still has its own inline implementations of all six handlers:

```python
# service.py:1708-1710
def _handle_health_check(self, params: dict[str, Any]) -> dict[str, Any]:
    """Агрегированный health check всех ключевых подсистем бэкенда."""
    return self._health_checker.check_all()
```

`HealthCheckService` is an orphan: it is defined, tested, but never instantiated by
`BackendService`. This creates two parallel implementations that can drift independently.
Specifically:

- `HealthCheckService.handle_ping` has an extensive doc comment: "КРИТИЧНО: не менять поля /
  типы — HealthMonitor.swift проверяет поле status == "ok" по каждому 3-секундному тику."
  `BackendService._handle_ping` lacks this guard comment entirely.
- `HealthCheckService.handle_health_check` (once W963 is merged) would call the fixed
  `_health_checker.check_all()`. The `BackendService._handle_health_check` already does the
  same — but this duplication means future fixes to `HealthCheckService` won't take effect
  unless `BackendService` is also updated.

The W953 audit noted `HealthCheckService` existed but classified it under "Production wire"
as correctly wired — this was incorrect. `HealthCheckService` was extracted but the
`BackendService` dispatch table still calls the old inline methods, not the service.

**Fix:** wire the six health-check IPC methods through `self._health_check_svc.*` (similar to
how `self._call_assist`, `self._history`, `self._translation` are wired) and remove the
duplicate inline implementations from `BackendService`.

---

### F4 — disk_space False Positives on Network-Mounted / Synthesized Filesystems (LOW)

**Location:** `KrabEar/backend/health_checker.py:113–140`
**Status:** OPEN (present on both codex/krab-ear-v2 and W963 branch)

`_check_disk_space` calls `shutil.disk_usage(str(check_path))`. On macOS, when `data_dir` is
on iCloud Drive (`~/Library/Mobile Documents/…`) or an NFS/SMB mount, `shutil.disk_usage`
returns the quota of the volume, not the local available space. When the user's iCloud quota is
nearly full (common on free 5 GB iCloud plans), this correctly fires a `"warning"` — which is
desirable.

However, the **false-positive** case occurs when macOS reports a fabricated free space for an
APFS container shared between the OS and data volumes. On APFS with multiple volumes sharing a
container (the default macOS Ventura/Sonoma layout), `shutil.disk_usage` on any path within the
container returns the container's total free space, not the volume's free space. A user with
`/System` and `~/Library` on the same APFS container who has set a macOS Storage Management
"purgeable space" reservation will see `shutil.disk_usage` report a stable high free_gb that
never reaches `DISK_WARN_GB=2.0` even when the physical disk is nearly full. This is a macOS
APFS purgeable-space accounting quirk.

Conversely, the `DISK_CRIT_GB=0.5` threshold can fire spuriously on a 128 GB SSD with 20 GB
free if the data_dir's parent path traverses an APFS volume with a small "local snapshot"
reserve (Time Machine local snapshots temporarily reduce reported free space).

**Fix:** Add a comment documenting APFS purgeable-space limitation. Optionally cross-check
with `statvfs` f_blocks × f_bsize to detect pathological under-reporting.

---

### F5 — No Test Coverage for warming_up → overall "degraded" Path on codex/krab-ear-v2 (LOW)

**Location:** `KrabEar/tests/test_health_checker.py` (codex/krab-ear-v2, 465 lines)
**Status:** OPEN — tests missing because F1 is unmerged

The W963 branch adds five tests covering the `warming_up` path (lines 152–202 of W963's test
file):

- `test_stt_warming_up_when_no_model_and_not_cached`
- `test_stt_ok_when_no_model_but_cached`
- `test_aggregate_warming_up_maps_to_degraded`
- `test_aggregate_warming_up_not_unhealthy`
- `test_check_all_degraded_when_stt_warming_up`

None of these exist on `codex/krab-ear-v2` (465-line file vs 520-line file on W963 branch).
Until PR #882 is merged, the production test suite has zero coverage for the cold-start
`warming_up` status returned by the fixed `_check_stt_model`.

Additionally, there is no test on either branch verifying that once `engine.current_model` is
set (simulating warmup completion), a subsequent call to `check_all()` transitions back to
`"healthy"` or `"degraded"` (not `"unhealthy"`). This transition test is missing.

**Fix:** merge PR #882 (includes the five warming_up tests). Add a separate
`test_stt_transitions_from_warming_up_to_ok` test to cover the model-load event.

---

## Confirmed Non-Issues

| Topic | Verdict |
|---|---|
| **IPC isolation (one check fail breaks all?)** | No. `handle_request` wraps the handler in a bare `try/except Exception` (service.py:1289–1292). Any exception from `check_all()` is caught and returned as `{"ok": false, "error": "internal_error"}`. Within `check_all()`, each of the five `_check_*` methods also has its own `try/except`, so a single check failure returns `{"status": "error"}` for that check but does not abort the other four checks. Isolation is solid. |
| **HealthMonitor.swift 3s ping interaction** | `handle_ping` returns a fixed `{"status": "ok"}` dict and does NOT call `HealthChecker.check_all()`. The Swift supervisor's liveness check is completely decoupled from the health-check subsystem. A `warming_up` STT status does not cause the Swift supervisor to restart the backend. |
| **Concurrent health checks / mutual exclusion** | `HealthChecker` holds no mutable state (only `_store`, `_transcriber`, `_llm_rewriter` references — all thread-safe by their own contracts). Concurrent calls to `check_all()` are safe. Verified by `test_concurrent_check_safe` (6 threads × 5 iterations). |

---

## Summary

| # | Title | Severity | W963 Fixes? |
|---|---|---|---|
| F1 | W963 fix (PR #882) not merged — false "ok" on cold start persists in production | HIGH | Yes, once merged |
| F2 | warming_up not in _aggregate_status degraded set (merge-order safety risk) | MED | Yes, once merged atomically |
| F3 | HealthCheckService is orphan — BackendService bypasses it with 6 inline duplicates | MED | No |
| F4 | disk_space false positives on APFS purgeable-space / local snapshots | LOW | No |
| F5 | Zero test coverage for warming_up path on codex/krab-ear-v2 | LOW | Yes, once merged |
