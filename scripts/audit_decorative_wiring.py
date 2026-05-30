#!/usr/bin/env python3
"""audit_decorative_wiring.py -- detect "decorative architecture" wiring bugs.

A "decorative architecture" bug occurs when:
  (a) A class defines a late-injection slot (field assigned None in __init__,
      used with a None-guard in other methods -> feature silently no-ops), AND
  (b) The hosting service (BackendService in service.py) never actually assigns
      the slot, so the collaborator stays None forever.

OR when:
  (c) An extracted service class exists in a module that is never imported or
      instantiated in service.py -- the class is "decorative architecture" because
      its handlers are still inline in service.py.

Usage:
    python scripts/audit_decorative_wiring.py [--strict] [--json]

Exit codes:
    0  no confirmed unwired late-injections found
    1  one or more CONFIRMED bugs found (suitable for CI gate)

Options:
    --strict  also report MED-severity candidates (default: HIGH only)
    --json    output results as JSON
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


CONFIRMED_BUGS: list[dict[str, Any]] = [
    # -------------------------------------------------------------------
    # HIGH: privacy / error-reporting / security
    # -------------------------------------------------------------------
    {
        "id": "W1686-F1",
        "severity": "HIGH",
        "module": "backend/disk_monitor.py",
        "class_name": "DiskSpaceMonitor",
        "field": "_error_bus",
        "issue": (
            "DiskSpaceMonitor._error_bus is never assigned in service.py. "
            "disk.warn and disk.critical KrabErrors are silently dropped -- "
            "they never reach ErrorBus or the Loud Errors UI toast."
        ),
        "fix": (
            "After `self._disk_monitor = DiskSpaceMonitor(...)` add: "
            "self._disk_monitor._error_bus = self._error_bus"
        ),
        # _literal_absent: if this literal is in service.py, bug is fixed
        "_literal_absent": "_disk_monitor._error_bus",
        "_check_file": "backend/service.py",
    },
    {
        "id": "W1686-F2",
        "severity": "HIGH",
        "module": "backend/event_replay.py",
        "class_name": "EventReplayManager",
        "field": "_settings_provider",
        "issue": (
            "EventReplayManager.settings_provider is never passed from service.py. "
            "When privacy_mode_enabled=True, event payloads in get_event_log are NOT "
            "redacted -- privacy mode is silently ignored for the event log."
        ),
        "fix": (
            "EventReplayManager(persist_path=..., "
            "settings_provider=self._settings_svc.cached_settings)"
        ),
        # Bug is fixed when EventReplayManager( call is on a line that also
        # contains settings_provider= OR on the lines immediately following it
        "_constructor_absent_kwarg": ("EventReplayManager(", "settings_provider"),
        "_check_file": "backend/service.py",
    },
    {
        "id": "W1686-F3",
        "severity": "HIGH",
        "module": "backend/error_reporter.py",
        "class_name": "ErrorReporter",
        "field": "_settings_provider",
        "issue": (
            "ErrorReporter._settings_provider is None (never passed from service.py). "
            "When privacy_mode_enabled=True, error messages in the ring-buffer are NOT "
            "redacted -- the privacy_mode guard is silently skipped."
        ),
        "fix": (
            "self._error_reporter = ErrorReporter("
            "settings_provider=self._settings_svc.cached_settings)"
        ),
        "_constructor_absent_kwarg": ("ErrorReporter(", "settings_provider"),
        "_check_file": "backend/service.py",
    },
    {
        "id": "W1686-F4",
        "severity": "HIGH",
        "module": "core/pipeline/stt_gigaam.py",
        "class_name": "_GigaAMSubprocessSession",
        "field": "_error_bus",
        "issue": (
            "_GigaAMSubprocessSession._error_bus is never assigned by GigaAMAdapter "
            "after session construction. GigaAM worker-timeout and crash errors "
            "(stt.gigaam_worker_timeout, stt.gigaam_worker_crashed) are silently "
            "dropped -- they never reach ErrorBus or the Loud Errors UI."
        ),
        "fix": (
            "In GigaAMAdapter._get_subprocess_session(), after "
            "session = _GigaAMSubprocessSession(...): "
            "session._error_bus = self._error_bus"
        ),
        "_literal_absent": "session._error_bus",
        "_check_file": "core/pipeline/stt_gigaam.py",
    },
    {
        "id": "W1686-F9",
        "severity": "HIGH",
        "module": "backend/health_check_service.py",
        "class_name": "HealthCheckService",
        "field": "(entire class -- orphaned extraction)",
        "issue": (
            "HealthCheckService (backend/health_check_service.py) is never imported "
            "or instantiated in service.py or ipc_dispatch.py. The extraction is "
            "decorative: ping, get_diagnostics, health_check, probe_llm_http, "
            "get_startup_diagnostics, check_integrity, handshake remain as inline "
            "methods in BackendService (~300 LOC overlap). MetricsCollector parameter "
            "in HealthCheckService.__init__ is also permanently None as a result."
        ),
        "fix": (
            "Instantiate HealthCheckService in service.py.__init__ and delegate "
            "the 7 IPC handlers to it (same pattern as AudioAnalyticsService, etc.)"
        ),
        "_orphan_check": True,
        "_orphan_symbol": "HealthCheckService",
        "_orphan_files": [
            "backend/service.py",
            "backend/ipc_dispatch.py",
        ],
    },
    # -------------------------------------------------------------------
    # MED: functional degradation (only reported with --strict)
    # -------------------------------------------------------------------
    {
        "id": "W1686-F5",
        "severity": "MED",
        "module": "backend/recap_scheduler.py",
        "class_name": "RecapScheduler",
        "field": "_settings_provider",
        "issue": (
            "RecapScheduler.settings_provider is never passed from service.py. "
            "The scheduler uses constructor defaults for recap_enabled / "
            "recap_time_hour / recap_email_to on every tick, ignoring runtime "
            "changes via set_settings IPC."
        ),
        "fix": (
            "RecapScheduler(..., settings_provider=self._settings_svc.cached_settings)"
        ),
        "_constructor_absent_kwarg": ("RecapScheduler(", "settings_provider"),
        "_check_file": "backend/service.py",
    },
    {
        "id": "W1686-F6",
        "severity": "MED",
        "module": "backend/export_scheduler.py",
        "class_name": "ExportScheduler",
        "field": "_settings_provider",
        "issue": (
            "ExportScheduler.settings_provider is never passed from service.py. "
            "When privacy_mode_enabled=True, the privacy guard in check_and_export() "
            "is silently skipped -- exports proceed even in privacy mode."
        ),
        "fix": (
            "ExportScheduler(data_dir=self.store.data_dir, "
            "settings_provider=self._settings_svc.cached_settings)"
        ),
        "_constructor_absent_kwarg": ("ExportScheduler(", "settings_provider"),
        "_check_file": "backend/service.py",
    },
    {
        "id": "W1686-F7",
        "severity": "MED",
        "module": "backend/archive_manager.py",
        "class_name": "ArchiveManager",
        "field": "_recording_chain_mgr",
        "issue": (
            "ArchiveManager._recording_chain_mgr is never wired from service.py "
            "(documented W1253 RC-3). When items are archived, their IDs are NOT "
            "removed from RecordingChain objects -- ghost item_id references in chains."
        ),
        "fix": (
            "After `self._archive_manager = ArchiveManager(...)`: "
            "self._archive_manager._recording_chain_mgr = self._chains"
        ),
        "_literal_absent": "_archive_manager._recording_chain_mgr",
        "_check_file": "backend/service.py",
    },
    {
        "id": "W1686-F8",
        "severity": "MED",
        "module": "backend/archive_manager.py",
        "class_name": "ArchiveManager",
        "field": "semantic_searcher (constructor kwarg)",
        "issue": (
            "ArchiveManager is instantiated without semantic_searcher= kwarg. "
            "When items are archived or unarchived, their semantic embeddings are "
            "NOT removed/re-indexed -- SemanticSearcher index drifts from archive."
        ),
        "fix": (
            "ArchiveManager(store=self.store, "
            "semantic_searcher=self._semantic_searcher)"
        ),
        # W1687: _semantic_searcher is created AFTER _archive_manager in __init__,
        # so a constructor kwarg is impossible — late-inject is the only option.
        # Scanner updated to use _literal_absent for the late-inject assignment.
        "_literal_absent": "_archive_manager._semantic_searcher",
        "_check_file": "backend/service.py",
    },
]


def _find_krabear_root(start: Path) -> Path:
    """Walk up from start until we find KrabEar/backend/service.py."""
    current = start.resolve()
    for _ in range(10):
        if (current / "KrabEar" / "backend" / "service.py").exists():
            return current
        current = current.parent
    raise RuntimeError(f"Could not find KrabEar root from {start}")


def _constructor_has_kwarg(src: str, constructor: str, kwarg: str) -> bool:
    """Return True if the constructor call site already includes the kwarg.

    Scans for `constructor` in src, then inspects the following ~8 lines
    for the presence of `kwarg`. This handles multi-line call sites.
    """
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if constructor in line:
            # Check this line and the next 8 for the kwarg
            block = "\n".join(lines[i : i + 9])
            if kwarg in block:
                return True
    return False


def _check_still_present(root: Path, bug: dict[str, Any]) -> bool:
    """Return True if the bug still exists in the current codebase."""

    if bug.get("_orphan_check"):
        symbol = bug["_orphan_symbol"]
        for rel in bug["_orphan_files"]:
            path = root / "KrabEar" / rel
            if path.exists() and symbol in path.read_text():
                return False  # symbol found -- wired, not a bug
        return True  # not found anywhere -- orphaned

    check_file = root / "KrabEar" / bug["_check_file"]
    if not check_file.exists():
        return False  # can't verify, assume fixed

    src = check_file.read_text()

    if "_literal_absent" in bug:
        # Bug is fixed when this exact string appears in the file
        return bug["_literal_absent"] not in src

    if "_constructor_absent_kwarg" in bug:
        constructor, kwarg = bug["_constructor_absent_kwarg"]
        # Bug is fixed when constructor call site includes the kwarg
        return not _constructor_has_kwarg(src, constructor, kwarg)

    return False


def main() -> int:
    strict = "--strict" in sys.argv
    json_output = "--json" in sys.argv

    try:
        root = _find_krabear_root(Path(__file__).parent)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    confirmed: list[dict[str, Any]] = []
    for bug in CONFIRMED_BUGS:
        if bug["severity"] == "MED" and not strict:
            continue
        if _check_still_present(root, bug):
            confirmed.append(bug)

    if json_output:
        output = {
            "confirmed_bugs": [
                {k: v for k, v in b.items() if not k.startswith("_")}
                for b in confirmed
            ],
            "count": len(confirmed),
        }
        print(json.dumps(output, indent=2))
    else:
        sev_note = "" if strict else " (HIGH only; use --strict for MED)"
        if not confirmed:
            print(
                f"audit_decorative_wiring: OK -- no unwired late-injection "
                f"bugs found{sev_note}"
            )
        else:
            print(
                f"audit_decorative_wiring: FAIL -- {len(confirmed)} confirmed"
                f" decorative-architecture bug(s){sev_note}\n"
            )
            for bug in confirmed:
                print(
                    f"  [{bug['severity']}] {bug['id']}  "
                    f"{bug['class_name']}.{bug['field']}"
                )
                print(f"         module : {bug['module']}")
                issue_s = bug["issue"]
                issue_short = issue_s[:115] + "..." if len(issue_s) > 115 else issue_s
                print(f"         issue  : {issue_short}")
                fix_s = bug["fix"]
                fix_short = fix_s[:115] + "..." if len(fix_s) > 115 else fix_s
                print(f"         fix    : {fix_short}")
                print()

    return 1 if confirmed else 0


if __name__ == "__main__":
    sys.exit(main())
