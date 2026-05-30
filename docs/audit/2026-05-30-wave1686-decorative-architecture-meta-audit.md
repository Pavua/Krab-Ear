# W1686 Decorative Architecture Meta-Audit

**Date:** 2026-05-30  
**Wave:** W1686  
**Auditor:** Sub-agent W1686 (meta-audit pass)  
**Scanner:** `scripts/audit_decorative_wiring.py` (new reusable CI script)

## Summary

This audit systematically scanned all `backend/*.py` and `core/*.py` modules for
the "decorative architecture" anti-pattern: a collaborator has a late-injection
slot (field set to `None` in `__init__`, guarded with `if self._X is None: return`)
but the wiring assignment is never made in `service.py`. The slot stays `None`
forever — the feature silently no-ops.

**9 confirmed bugs found** (5 HIGH, 4 MED). This is separate from the ~8 already
fixed in prior waves (W1622 StartupDiagnostics, W1652 recorder, W1677 event_bus,
W1662 AutoGlossary, W1676 MetricsCollector/get_metrics_dashboard, W1412 AutoDeduplicator).

## Confirmed Bugs

### HIGH Severity

#### W1686-F1 — DiskSpaceMonitor._error_bus (never wired)

- **File:** `backend/disk_monitor.py`
- **Slot:** `self._error_bus: Any | None = None` (line 63)
- **Guard:** `if error_bus is None: return` (lines 287, 316)
- **Impact:** `disk.warn` and `disk.critical` KrabErrors are silently dropped — they
  never reach `ErrorBus` or the Loud Errors UI toast. Users get no notification of
  low disk space even when Phase B error codes were specifically added for this.
- **Fix:** After `self._disk_monitor = DiskSpaceMonitor(...)` in `service.py.__init__`:
  ```python
  self._disk_monitor._error_bus = self._error_bus
  ```

#### W1686-F2 — EventReplayManager._settings_provider (never passed)

- **File:** `backend/event_replay.py`
- **Slot:** `self._settings_provider = settings_provider` — accepts `None` default
- **Guard:** `if self._settings_provider is None: return False` (line 79)
- **Impact:** When `privacy_mode_enabled=True`, event payloads logged to `get_event_log`
  are NOT redacted. The privacy mode is silently ignored for the entire event log.
  Users who enable privacy mode expect all stored event data to be redacted.
- **Fix:** Pass `settings_provider=` at construction:
  ```python
  self._event_replay = EventReplayManager(
      persist_path=self.store.data_dir / "event_replay.ndjson",
      settings_provider=self._settings_svc.cached_settings,
  )
  ```

#### W1686-F3 — ErrorReporter._settings_provider (never passed)

- **File:** `backend/error_reporter.py`
- **Slot:** `self._settings_provider = settings_provider` — accepts `None` default
- **Guard:** `if self._settings_provider is None: return False` (line 79)
- **Impact:** When `privacy_mode_enabled=True`, error messages stored in the
  `ErrorReporter` ring-buffer are NOT redacted to `<redacted: privacy_mode>`.
  `get_error_report` IPC leaks error content in privacy mode.
- **Fix:**
  ```python
  self._error_reporter = ErrorReporter(
      settings_provider=self._settings_svc.cached_settings
  )
  ```

#### W1686-F4 — _GigaAMSubprocessSession._error_bus (never wired by GigaAMAdapter)

- **File:** `core/pipeline/stt_gigaam.py`
- **Slot:** `self._error_bus: Optional[object] = None` (line 536 in `_GigaAMSubprocessSession`)
- **Guard:** `_error_bus = getattr(self, "_error_bus", None); if _error_bus is not None:` (lines 558, 635, 697, 861)
- **Impact:** `stt.gigaam_worker_timeout`, `stt.gigaam_worker_crashed`, and
  `stt.gigaam_oom_restart` KrabErrors are silently dropped. GigaAM subprocess
  failures never appear in the Loud Errors UI.
- **Root cause:** `GigaAMAdapter._get_subprocess_session()` (line 352) creates
  `_GigaAMSubprocessSession` but does NOT set `session._error_bus`. `GigaAMAdapter`
  itself receives no `error_bus` — the chain is broken from `service.py`.
- **Fix (two-step):**
  1. In `STTRouter.get_gigaam_adapter()`, after adapter creation, assign error bus
     to the adapter (requires STTRouter to receive `error_bus` from service.py).
  2. In `GigaAMAdapter._get_subprocess_session()`, after `session = _GigaAMSubprocessSession(...)`:
     ```python
     session._error_bus = self._error_bus
     ```

#### W1686-F9 — HealthCheckService entire class (orphaned extraction)

- **File:** `backend/health_check_service.py`
- **Impact:** `HealthCheckService` is never imported or instantiated anywhere in
  `service.py` or `ipc_dispatch.py`. The class is a fully decorative extraction:
  - `ping`, `get_diagnostics`, `health_check`, `probe_llm_http`,
    `get_startup_diagnostics`, `check_integrity`, `handshake` remain as inline
    methods in `BackendService` (~300 LOC).
  - The `MetricsCollector` parameter in `HealthCheckService.__init__` is permanently
    `None` since the class is never instantiated from service.py.
  - Unit tests exist (`test_health_check_service.py`) but test an orphaned class.
- **Fix:** Instantiate and wire in `service.py.__init__`:
  ```python
  from backend.health_check_service import HealthCheckService
  self._health_check_svc = HealthCheckService(
      store=self.store,
      health_checker=self._health_checker,
      startup_diagnostics=self._startup_diagnostics,
      integrity_checker=self._integrity_checker,
      llm_probe=self._llm_probe,
      metrics_collector=MetricsCollector(),
      transcriber=self.transcriber,
      llm_rewriter=self._llm_rewriter,
      settings_svc=self._settings_svc,
      start_time=self._start_time,
      app_version=settings.APP_VERSION,
      recorder=self.recorder,
      last_stt_engine_ref=self._last_stt_engine_ref,
  )
  ```
  Then delegate the 7 IPC methods in `ipc_dispatch.py`.

---

### MED Severity (functional degradation)

#### W1686-F5 — RecapScheduler._settings_provider (never passed)

- **File:** `backend/recap_scheduler.py`
- **Impact:** `RecapScheduler` uses constructor defaults for `recap_enabled`,
  `recap_time_hour`, and `recap_email_to` on every tick. Runtime changes via
  `set_settings { "recap_enabled": true }` are silently ignored.
- **Fix:** `RecapScheduler(..., settings_provider=self._settings_svc.cached_settings)`

#### W1686-F6 — ExportScheduler._settings_provider (never passed)

- **File:** `backend/export_scheduler.py`
- **Impact:** Privacy guard in `check_and_export()` (line 409) is silently skipped.
  Auto-exports proceed even when `privacy_mode_enabled=True`.
- **Fix:** `ExportScheduler(data_dir=self.store.data_dir, settings_provider=self._settings_svc.cached_settings)`

#### W1686-F7 — ArchiveManager._recording_chain_mgr (never wired)

- **File:** `backend/archive_manager.py`
- **Slot:** `self._recording_chain_mgr = None` (line 53, marked W1253 RC-3)
- **Guard:** `if self._recording_chain_mgr is not None:` (line 151)
- **Impact:** When history items are archived, their IDs are NOT removed from
  `RecordingChain` objects. Ghost item_id references remain in chains.
- **Fix:** After `self._archive_manager = ArchiveManager(store=self.store)`:
  ```python
  self._archive_manager._recording_chain_mgr = self._chains
  ```

#### W1686-F8 — ArchiveManager.semantic_searcher (not passed at construction)

- **File:** `backend/archive_manager.py`
- **Impact:** When items are archived or unarchived, semantic embeddings are NOT
  removed/re-indexed. `SemanticSearcher` index drifts from the actual archive state —
  archived items remain searchable via semantic search.
- **Fix:** `ArchiveManager(store=self.store, semantic_searcher=self._semantic_searcher)`

---

## Scanner

`scripts/audit_decorative_wiring.py` — reusable, exits 1 if confirmed bugs found.

```bash
# HIGH-only (CI default)
python scripts/audit_decorative_wiring.py

# Include MED severity
python scripts/audit_decorative_wiring.py --strict

# JSON output
python scripts/audit_decorative_wiring.py --strict --json
```

Detection strategy:
- `_literal_absent`: checks that a specific wiring string (e.g. `._disk_monitor._error_bus`)
  appears in the target file
- `_constructor_absent_kwarg`: checks that the constructor call site and the required
  kwarg appear within 8 lines of each other (handles multi-line constructors)
- `_orphan_check`: checks that a class symbol is absent from all expected wiring files

## Previously Fixed (reference)

| Bug | Wave | Fix |
|-----|------|-----|
| `StartupDiagnostics._error_bus` | W1622 | `self._startup_diagnostics._error_bus = self._error_bus` |
| `AudioRecorder._error_bus` | W1652 | Wired in `BackendService.__init__` |
| `EventBus._event_replay` | W1677 | `event_bus._event_replay = self._event_replay` |
| `AutoGlossaryBuilder.settings_provider` | W1662 | Passed at construction |
| `MetricsCollector` never called by `get_metrics_dashboard` | W1676 F3 | Handler updated |
| `HealthCheckService._metrics_collector` unused | W1676 F5 | Moot (class orphaned per F9) |
| `AutoDeduplicator.settings_provider` | W1412 | Passed at construction |
| `CalendarLinker` (dead) | W1412 | No late-inject slots confirmed |
