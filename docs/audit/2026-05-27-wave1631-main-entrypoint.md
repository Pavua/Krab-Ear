# Wave 1631 — Backend main.py entrypoint audit

First-pass audit of `KrabEar/main.py` (thin shim — all startup logic lives in
`backend/service.py::main()`) and the launchd Variant B plist template.
File never directly audited during the 1600-wave marathon.

Source files examined:
- `KrabEar/main.py` (13 lines — imports and delegates to `backend.service.main`)
- `KrabEar/backend/service.py` lines 4317–4372 (`main()`, `configure_logging()`,
  `build_service()`, `default_data_dir()`)
- `KrabEar/backend/observability.py` (`init_sentry()`, `install_signal_handlers()`)
- `KrabEar/backend/shutdown_handler.py` (`GracefulShutdownHandler`)
- `KrabEar/launchagents/ai.krab.ear.backend.plist.template`
- `KrabEar/tests/test_shutdown_handler_wired_in_main.py`

---

## F1 HIGH — `GracefulShutdownHandler.register()` and `shutdown()` never called in `main()`

**Location**: `service.py` lines 4354–4368 (`main()`), `shutdown_handler.py` lines 71–92
(`register()`), lines 94–155 (`shutdown()`).

`GracefulShutdownHandler` is instantiated in `BackendService.__init__` at line 595 and
stored as `self._shutdown_handler`.  Its `register(service)` method must be called to
wire SIGTERM/SIGINT and to give the handler a reference to the service for the 6-step
shutdown sequence (vocabulary save, audit log flush, usage stats, playback stats, history
compaction, socket close + `shutdown_info.json` write).

`main()` never calls either:

```python
service._shutdown_handler.register(service)   # absent
service._shutdown_handler.shutdown()           # absent from _signal_handler and finally
```

The current `_signal_handler` in `main()` only calls `server.stop()` and
`service.close()`.  `service.close()` stops the LLM probe and the export-scheduler
thread but performs none of the 6 shutdown steps in `GracefulShutdownHandler.shutdown()`.
On SIGTERM the vocabulary, audit log, usage stats, playback stats, and
`shutdown_info.json` are silently skipped.

**Evidence**: `test_shutdown_handler_wired_in_main.py` (added in W981) contains four
tests that assert `_shutdown_handler.register(` and `_shutdown_handler.shutdown()` are
present in the `main()` source text (`test_register_called_in_main`,
`test_shutdown_called_in_signal_handler`, `test_shutdown_called_in_finally_block`,
`test_ipc_server_assigned_before_register`).  All four tests are static source-text
assertions against the AST of `service.py::main()` — and all four fail because the
calls are absent.

**Impact**: clean shutdown sequence is dead code in production. `shutdown_info.json`
never written, so `get_shutdown_status` always returns `clean=None`.

**Fix**: in `main()`, after `server = IPCServer(...)`, add:
```python
service._ipc_server = server
service._shutdown_handler.register(service)
```
Update `_signal_handler` to call `service._shutdown_handler.shutdown()` instead of
`service.close()`, and update the `finally` block similarly.

---

## F2 HIGH — `init_sentry()` called without `settings` dict — `privacy_mode_enabled` not honoured at startup

**Location**: `service.py` lines 4336–4340 (`main()`), `observability.py` lines 223–302
(`init_sentry()`).

`init_sentry()` accepts an optional `settings` dict.  When supplied and
`settings["privacy_mode_enabled"]` is `True`, the function returns `False` without
initialising the SDK — the intended privacy contract.

`main()` calls:
```python
sentry_ok = init_sentry(
    dsn=settings.SENTRY_DSN or None,
    environment=settings.SENTRY_ENVIRONMENT,
    release=get_release_string(),
)
```

The `settings=` argument is omitted.  The `settings` variable here is the Pydantic
`core.config.Settings` singleton (env-var driven), which has no `privacy_mode_enabled`
attribute — that flag lives exclusively in `settings.json` via `DEFAULT_SETTINGS`
(config.py line 988).  There is no `KRAB_EAR_PRIVACY_MODE_ENABLED` env var.

**Result**: a user who enabled privacy mode via the IPC `set_settings` API and then
restarts the backend will have Sentry initialised anyway on next startup, until the
runtime `SettingsService` re-invokes `init_sentry` with the current settings dict
(which happens only on the `_on_privacy_mode_off` hook — the inverse direction).

**Fix**: in `main()`, read the persisted settings before calling `init_sentry()`:
```python
from backend.state_store import StateStore as _SS  # already imported indirectly
_persisted = _SS(data_dir=data_dir).load_settings() or {}
sentry_ok = init_sentry(
    dsn=settings.SENTRY_DSN or _persisted.get("sentry_dsn") or None,
    environment=settings.SENTRY_ENVIRONMENT,
    release=get_release_string(),
    settings=_persisted,
)
```
Alternatively, move Sentry init into `build_service()` after settings are loaded.

---

## F3 MED — SIGTERM handler conflict: `install_signal_handlers()` overwritten by `main()`

**Location**: `service.py` lines 4348, 4362–4363.

`install_signal_handlers()` (from `observability.py`) installs a Sentry-aware handler
for SIGTERM that calls `sentry_sdk.flush(timeout=2.0)` before re-raising.  It is called
at line 4348.

Eight lines later, `main()` unconditionally re-installs its own `_signal_handler` for
both SIGINT and SIGTERM:
```python
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)
```

This overwrites the Sentry handler for SIGTERM.  When the process receives SIGTERM in
production (e.g. launchd bootout, HealthMonitor SIGTERM), no Sentry breadcrumb or flush
occurs.

**Fix**: merge the Sentry flush into `_signal_handler` before delegating to
`server.stop()` / `shutdown()`, then remove the `install_signal_handlers()` call for
SIGTERM (keep it only for SIGABRT/SIGSEGV which `main()` does not override).

---

## F4 MED — Log level hardcoded to `INFO`; no `--log-level` CLI flag

**Location**: `service.py` line 4274 (`configure_logging()`), `core/config.py` line 172.

`configure_logging()` always calls `logging.basicConfig(level=logging.INFO, ...)`.
`LOG_FORMAT` is respected (env var `KRAB_EAR_LOG_FORMAT`), but there is no equivalent
`LOG_LEVEL` setting and no `--log-level` CLI argument.

`KRAB_EAR_LOG_FORMAT=json` is set in the launchd plist template (via
`EnvironmentVariables`) and propagates correctly.  However, enabling DEBUG logging for
production troubleshooting requires source edits.

**Note**: This is a low-urgency convenience gap, not a safety issue — `logging.INFO` is
a sane production default.

**Fix**: add `LOG_LEVEL: str = "INFO"` to `core/config.py Settings`, read it in
`configure_logging()`:
```python
level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
logging.basicConfig(level=level, handlers=handlers)
```

---

## F5 LOW — `main.py` itself is untested; all tests target `service.py` directly

**Location**: `KrabEar/main.py`, `KrabEar/tests/`.

`main.py` is 13 lines and does nothing beyond `from backend.service import main` /
`if __name__ == "__main__": main()`.  No test file imports or exercises `KrabEar.main`
directly.  All tests that exercise startup behaviour import `backend.service` directly.

The launchd plist template invokes `backend/service.py` as `__main__` (line 27 of the
template: `KrabEar/backend/service.py`), bypassing `KrabEar/main.py` entirely.  The
`Start Krab Ear.command` and `CLAUDE.md` documentation reference
`python KrabEar/main.py --data-dir ...`.

**Impact**: zero — `main.py` has one non-trivial execution path, and it is covered by
`test_shutdown_handler_wired_in_main.py` which tests `service.main()` directly.  The
discrepancy between the documented entrypoint (`main.py`) and the launchd entrypoint
(`service.py`) is cosmetic.

**Noted**: the plist template correctly sets `PYTHONPATH=__PROJECT_ROOT__/KrabEar`
so both invocation paths resolve imports identically.

---

## Summary

| # | Severity | Finding |
|---|---|---|
| F1 | HIGH | `GracefulShutdownHandler.register()` / `shutdown()` absent in `main()` — 6-step clean shutdown is dead code |
| F2 | HIGH | `init_sentry()` ignores `privacy_mode_enabled` from `settings.json` at startup |
| F3 | MED | `install_signal_handlers()` SIGTERM handler immediately overwritten by `main()`'s own handler |
| F4 | MED | Log level hardcoded to `INFO`; no `--log-level` flag or `LOG_LEVEL` env var |
| F5 | LOW | `main.py` not tested directly; launchd plist bypasses it entirely (cosmetic gap) |
