# Wave 978 Audit — ExportScheduler

**File**: `KrabEar/backend/export_scheduler.py`
**Auditor**: W978 (sub-agent)
**Date**: 2026-05-26
**Severity key**: CRIT / HIGH / MED / LOW / INFO

---

## Executive Summary

`ExportScheduler` is opportunistic — no background thread; exports happen only when `check_and_export()` is called explicitly. The implementation is generally solid but has **5 confirmed findings**: the scheduler is effectively dead in production (never triggered), path traversal is unconstrained for user-configured destinations, the export file write is not atomic (no fsync/rename), privacy mode is not respected, and Sentry breadcrumbs are absent.

---

## Findings

### F1 — CRIT: `check_and_export` is never called in production

**File**: `KrabEar/backend/service.py`, `KrabEar/backend/export_scheduler.py`

`ExportScheduler` is instantiated at line 357 of `service.py` and three IPC methods are wired (`configure_auto_export`, `get_export_schedule_status`, `list_auto_exports`). However, `check_and_export(store)` — the method that actually performs the export — is **never called** anywhere outside of tests:

```
$ grep -rn "check_and_export" KrabEar/ | grep -v test
KrabEar/backend/export_scheduler.py:4:check_and_export() — без фоновых потоков.
KrabEar/backend/export_scheduler.py:31:    только при явном вызове check_and_export().
KrabEar/backend/export_scheduler.py:341:    def check_and_export(self, store: Any) -> dict | None:
```

The docstring says "opportunistic — export happens only on explicit `check_and_export()` call", but no caller exists in `service.py`, `main.py`, or native code. Users can configure a schedule and enable it, but exports will never fire. The feature is silently inoperative.

**Fix**: Wire `check_and_export` to a periodic hook — either inside `handle_request` (post-dispatch, e.g., after `transcribe`/`paste`) or in a lightweight background tick in `BackendService.__init__`.

---

### F2 — HIGH: Path traversal — `output_dir` not validated against `data_dir`

**File**: `KrabEar/backend/export_scheduler.py`, lines 85–89

```python
def _effective_output_dir(self, schedule: dict) -> Path:
    if schedule.get("output_dir"):
        return Path(schedule["output_dir"])   # ← raw user path
    return self.exports_dir
```

`output_dir` is stored as-is from the IPC `configure_auto_export` call (service.py line 1368: `output_dir = params.get("output_dir")`). No validation against `data_dir`, no `resolve()` + `is_relative_to()` guard. A caller can supply `/etc/cron.d/krab` or any world-readable path and the scheduler will happily write exports there. While the Unix socket is local-only (reduces exposure), a compromised process or a malicious IPC client can exfiltrate data to arbitrary paths.

**Fix**:
```python
def _effective_output_dir(self, schedule: dict) -> Path:
    if schedule.get("output_dir"):
        p = Path(schedule["output_dir"]).resolve()
        if not p.is_relative_to(self.data_dir.resolve()):
            logger.warning("output_dir вне data_dir, игнорируем: %s", p)
            return self.exports_dir
        return p
    return self.exports_dir
```
Or restrict to an allow-list of user-accessible directories (e.g., Desktop, Documents).

---

### F3 — MED: Export file write is not atomic (no fsync, no tmp+rename)

**File**: `KrabEar/backend/export_scheduler.py`, lines 137–138

```python
content = self._generate_content(store, fmt)
file_path.write_text(content, encoding="utf-8")   # ← direct write, no tmp+rename
```

`_save_schedule()` (line 78–83) correctly does tmp+rename. But `_do_export()` writes the export file directly. If the process crashes or disk fills mid-write, the output file will be left truncated/corrupt and no recovery mechanism exists. For large JSON exports (5000 items) a partial file is indistinguishable from a complete one.

**Fix**:
```python
tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
tmp_path.write_text(content, encoding="utf-8")
import os
with tmp_path.open("rb") as f:
    os.fsync(f.fileno())
tmp_path.replace(file_path)
```

---

### F4 — MED: Privacy mode not respected — exports history when privacy is enabled

**File**: `KrabEar/backend/export_scheduler.py`, `KrabEar/backend/service.py`

The backend tracks `privacy_mode_enabled` as a settings key (see `core/config.py` line 987 and `DEFAULT_SETTINGS`). When privacy mode is active, history recording and paste are suppressed in the pipeline. However, `ExportScheduler.check_and_export()` and `_generate_content()` call `store.get_history_page_filtered()` unconditionally without any privacy-mode guard. A user enabling privacy mode would reasonably expect auto-exports to stop (or at minimum, be skipped).

`BackendService._handle_configure_auto_export` also does not consult privacy mode. The existing `configure_auto_export` IPC handler (lines 1354–1375) has no guard.

**Fix**: In `check_and_export()`, read the current privacy-mode setting (via `self._get_runtime_setting("privacy_mode_enabled", False)`) and return `None` early when active. Alternatively, guard in the service-layer caller once F1 is fixed.

---

### F5 — LOW: No Sentry breadcrumbs for export success or failure

**File**: `KrabEar/backend/export_scheduler.py`

`add_breadcrumb` / `capture_exception` are absent entirely. The service layer wraps export calls in no try/except either (since `check_and_export` is never called — F1). When F1 is fixed, failed exports will silently propagate as untracked `IOError`/`OSError`, and successful exports won't appear in Sentry traces.

Pattern used by other modules (`backend/observability.py`):
```python
from backend.observability import add_breadcrumb, capture_exception

# In check_and_export():
try:
    entry = self._do_export(store, fmt, output_dir)
    add_breadcrumb("export_scheduler", "auto_export_ok",
                   {"format": fmt, "size_bytes": entry["size_bytes"]})
except Exception as exc:
    capture_exception(exc)
    raise
```

---

## Non-findings (checked, OK)

| Check | Result |
|-------|--------|
| **Scheduler correctness / DST** | `check_and_export` uses `datetime.now(timezone.utc)` and stores ISO-8601 with offset. UTC throughout — no DST exposure. Elapsed-hours comparison uses `total_seconds()`. Clean. |
| **Settings staleness** | `interval_hours`, `format`, `output_dir`, and `enabled` are all loaded fresh from `export_schedule.json` on every `check_and_export()` call (line 351: `schedule = self._load_schedule()`). No baking at `__init__`. Sister concern (W918/W933) does not apply here. |
| **ENOSPC handling** | `_do_export` does not explicitly catch `OSError(ENOSPC)`, but `IOError` propagates up to the caller (BackendService), which is the correct layer for error handling. Tests in `test_export_scheduler_extras.py` (TestHandlesUnwritableDiskGracefully) verify propagation. Acceptable given the opportunistic design. |
| **Concurrent export + recording** | `_lock` is held for the entire `check_and_export` body including `_do_export`. The barrier test in `TestConcurrentTriggerSerialized` confirms only one export fires when two threads race. |
| **Thread lifecycle** | No background thread to manage — by design. The lock exists for multi-caller IPC safety. |
| **Test coverage** | Good: `test_export_scheduler.py` (52 cases) + `test_export_scheduler_extras.py` (Wave 211, 36 cases) cover configure/cancel/prune/formats/unicode/concurrency/atomic-schedule-write/unwritable-disk. |

---

## Summary table

| # | Severity | Description |
|---|----------|-------------|
| F1 | CRIT | `check_and_export` never called — feature silently inoperative in production |
| F2 | HIGH | `output_dir` not validated — path traversal to arbitrary filesystem paths |
| F3 | MED | Export file write not atomic (no fsync + tmp/rename) |
| F4 | MED | Privacy mode not respected — exports history even when privacy is enabled |
| F5 | LOW | No Sentry breadcrumbs for export success or failure |
