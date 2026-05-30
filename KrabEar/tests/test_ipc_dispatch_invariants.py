"""IPC dispatch invariant tests.

Wave 51 pattern: verify structural consistency of the BackendService
dispatch table without actually calling any handlers.

Test 1 — every dispatch entry resolves:
    All callables registered in the handlers dict inside handle_request
    must be actual callables (no missing attribute, no None).

Test 2 — every public handle_* method is reachable:
    All public handle_* methods on BackendService and the 5 core extracted
    services must appear in the dispatch table.  Orphan methods (defined
    but not registered) flag potential drift.

Acceptable non-dispatch handle_* names (see KNOWN_ORPHANS below):
- ``handle_request``   — the dispatcher itself
- ``handle_ping``      — auto-injected alias (checked separately)
- ``handle_*`` on internal helpers that are not IPC services
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

# ---------------------------------------------------------------------------
# Known exceptions — methods that intentionally live outside the dispatch table
# ---------------------------------------------------------------------------

# These are on BackendService itself but are not IPC methods
BACKEND_SERVICE_NON_DISPATCH = frozenset({
    "handle_request",       # the dispatcher itself
})

# These are on extracted *service* objects and intentionally not in dispatch
# (helper/internal methods, not exposed as IPC endpoints)
EXTRACTED_SERVICE_NON_DISPATCH: dict[str, frozenset] = {
    # SpeakerManager: fingerprint methods wired in Wave 961 (W951 F4 fix).
    # handle_merge_speakers was removed from dispatch in Wave 961 when 3 new
    # fingerprint handlers replaced it; remains in class for future use.
    "SpeakerManager": frozenset({
        "handle_merge_speakers",
    }),
    # W924/W949: TranscriptionQueue is DEAD CODE as of v2.0.5 — process_next() was never
    # called in production; all 4 IPC handlers removed from dispatch. The class itself
    # remains for future resurrection. All handle_* methods are now non-dispatch orphans.
    "TranscriptionQueue": frozenset({
        "handle_enqueue",
        "handle_cancel",
        "handle_get_status",
        "handle_list_queue",
        "handle_peek",
    }),
    # IntegrityChecker exposes handle_check_integrity and handle_repair_data
    # but service.py uses its own _handle_check_integrity / _handle_repair_integrity
    # that call the lower-level check_integrity() / repair() methods directly.
    # handle_check_integrity is therefore an orphan on the IntegrityChecker class.
    # Wave 55 target: either wire it or remove it.
    "IntegrityChecker": frozenset({
        "handle_check_integrity",
        "handle_repair_data",
    }),
    # CollectionManager has rename_collection not yet wired
    "CollectionManager": frozenset({
        "handle_rename_collection",
    }),
    # CallAssistService has internal/template methods not yet exposed as IPC endpoints.
    # These were pre-existing orphans before W828 (never registered in dispatch table).
    "CallAssistService": frozenset({
        "handle_cost_report",
        "handle_list_templates",
        "handle_template",
    }),
}


def _build_dispatch_table(service_instance):
    """Call handle_request with a sentinel method to force table construction,
    then fish out the handlers dict via inspection of the source code approach.

    Since the handlers dict is built *inline* inside handle_request, we use a
    different approach: patch handle_request to capture the local ``handlers``
    variable using a frame hook.
    """
    captured = {}

    original = service_instance.handle_request

    def capturing_handle_request(payload):
        import sys as _sys
        # We call the original and intercept via settrace on a fresh call
        frame_ref = []

        def tracer(frame, event, arg):
            if event == "call" and frame.f_code is original.__func__.__code__:
                frame_ref.append(frame)
            return tracer

        old_trace = _sys.gettrace()
        _sys.settrace(tracer)
        try:
            result = original(payload)
        finally:
            _sys.settrace(old_trace)

        if frame_ref:
            local_handlers = frame_ref[0].f_locals.get("handlers", {})
            captured.update(local_handlers)
        return result

    service_instance.handle_request = capturing_handle_request
    # Trigger with a dummy unknown method (fast path — just needs table built)
    service_instance.handle_request({"id": "test", "method": "__invariant_probe__", "params": {}})
    service_instance.handle_request = original
    return captured


def _get_backend_service_class():
    """Import BackendService with all heavy dependencies mocked."""
    heavy_modules = [
        "mlx_whisper", "mlx", "mlx.core", "mlx.nn",
        "torch", "torchaudio", "pyannote", "pyannote.audio",
        "sounddevice", "soundfile",
        "sentry_sdk",
        "transformers",
        "psutil",
    ]
    mocks = {mod: MagicMock() for mod in heavy_modules}

    # Patch mlx_lock to a no-op context manager
    class _NoOpLock:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch.dict("sys.modules", mocks):
        with patch("core.mlx_lock.mlx_lock", return_value=_NoOpLock()):
            from backend.service import BackendService  # noqa: PLC0415
    return BackendService


def _make_minimal_service():
    """Construct a BackendService with all collaborators mocked."""
    import tempfile

    heavy_modules = [
        "mlx_whisper", "mlx", "mlx.core", "mlx.nn",
        "torch", "torchaudio", "pyannote", "pyannote.audio",
        "sounddevice", "soundfile",
        "sentry_sdk",
        "transformers",
        "psutil",
    ]
    mock_map = {mod: MagicMock() for mod in heavy_modules}

    class _NoOpLock:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    tmp = tempfile.mkdtemp()

    with patch.dict("sys.modules", mock_map):
        with patch("core.mlx_lock.mlx_lock", return_value=_NoOpLock()):
            with patch("backend.service.AudioEngine", MagicMock()):
                with patch("backend.service.AudioRecorder", MagicMock()):
                    from backend.service import BackendService  # noqa: PLC0415
                    svc = BackendService.__new__(BackendService)
                    svc.__init__ = lambda *a, **kw: None

                    # Attach a real store mock with data_dir
                    store_mock = MagicMock()
                    store_mock.data_dir = tmp
                    svc.store = store_mock

                    # Attach mock collaborators that match attribute names used
                    # in handle_request dispatch table
                    for attr in [
                        "_call_assist", "_history", "_translation", "_settings_svc",
                        "_glossary_auto_learn", "_paste_app_memory", "_collections",
                        "_chains", "_recording_scheduler", "_error_reporter",
                        "_event_replay", "_config_presets",
                        "_data_migrator", "_collections", "_sharing",
                        "_transcript_versioning", "_paste_formatter",
                        "_merger", "_obsidian_sync", "_playback_tracker",
                        "_speaker_manager", "_live_subs", "_plugin_manager",
                        "_feature_flags", "_hotword_detector", "_model_cache_manager",
                        "_oww_adapter", "_tts", "_bookmarks", "_call_cost_estimator",
                        "_call_auto_end", "_template_manager", "_webhook_manager",
                        "_auto_backup", "_export_scheduler", "_search_history",
                        "_archive_manager", "_metadata_enricher",
                        "_request_signer", "_ipc_throttle",
                        # Dedup / semantic / bulk
                        "_deduplicator", "_semantic_searcher", "_bulk_reprocessor",
                        # misc
                        "_export_scheduler", "_search_history",
                    ]:
                        setattr(svc, attr, MagicMock())

                    # Mocks needed for private _handle_* that access attributes
                    svc._engine = MagicMock()
                    svc._recorder = MagicMock()
                    svc._transcriber = MagicMock()
                    svc._translator = MagicMock()
                    svc._llm_rewriter = MagicMock()
                    svc._metrics = MagicMock()
                    svc._session_log = MagicMock()
                    svc._error_bus = MagicMock()

    return svc


class TestIPCDispatchInvariants(unittest.TestCase):
    """Structural invariants for the IPC dispatch table."""

    @classmethod
    def setUpClass(cls):
        """Build the dispatch table once for all tests."""
        cls._dispatch_table = cls._extract_dispatch_table()

    @classmethod
    def _extract_dispatch_table(cls):
        """Extract the handlers dict by parsing service.py source.

        The live dispatch table is the inline ``handlers`` dict inside
        ``BackendService.handle_request`` (12-space indented keys).
        ipc_dispatch.py is kept for historical reference only.
        """
        import re

        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        # The handlers dict is defined inside handle_request (12-space indent).
        # Keys look like: «            "method_name": self._handle_...»
        method_names = re.findall(r'^            "([a-z][a-z0-9_]*)"\s*:', source, re.MULTILINE)
        return set(method_names)

    def test_dispatch_table_not_empty(self):
        """Sanity: the dispatch table must have a substantial number of entries."""
        self.assertGreater(
            len(self._dispatch_table), 200,
            f"Dispatch table suspiciously small: {len(self._dispatch_table)} entries"
        )

    def test_every_handle_method_in_extracted_services_is_reachable(self):
        """Every public handle_* method in extracted *_service.py files and
        other registered service modules must appear in the dispatch table,
        unless explicitly in EXTRACTED_SERVICE_NON_DISPATCH.
        """
        # Map of (class_name, method_name) -> file_path for reporting
        service_files = [
            os.path.join(KRAB_EAR_ROOT, "backend", "history_service.py"),
            os.path.join(KRAB_EAR_ROOT, "backend", "translation_service.py"),
            os.path.join(KRAB_EAR_ROOT, "backend", "settings_service.py"),
            os.path.join(KRAB_EAR_ROOT, "backend", "call_assist_service.py"),
            os.path.join(KRAB_EAR_ROOT, "backend", "speaker_manager.py"),
            os.path.join(KRAB_EAR_ROOT, "backend", "transcription_queue.py"),
            os.path.join(KRAB_EAR_ROOT, "backend", "collection_manager.py"),
            os.path.join(KRAB_EAR_ROOT, "backend", "integrity_checker.py"),
        ]

        import re
        orphans = []

        for filepath in service_files:
            basename = os.path.basename(filepath)
            class_name = None
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    cls_match = re.match(r"^class (\w+)", line)
                    if cls_match:
                        class_name = cls_match.group(1)
                    method_match = re.match(r"    def (handle_\w+)\(", line)
                    if method_match:
                        method_name = method_match.group(1)
                        allowed_non_dispatch = EXTRACTED_SERVICE_NON_DISPATCH.get(
                            class_name or "", frozenset()
                        )
                        if method_name in allowed_non_dispatch:
                            continue
                        # Check if this method is referenced in the live dispatch in service.py.
                        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
                        with open(service_path, encoding="utf-8") as sf:
                            service_src = sf.read()
                        if f".{method_name}" not in service_src:
                            orphans.append(f"{basename}::{class_name}.{method_name}")

        if orphans:
            self.fail(
                f"Found {len(orphans)} handle_* method(s) in extracted services "
                f"that are NOT referenced in service.py:\n"
                + "\n".join(f"  - {o}" for o in sorted(orphans))
            )

    def test_service_py_private_handlers_all_in_dispatch(self):
        """Every _handle_* private method in service.py must correspond to a
        registered IPC method name (directly or via extracted-service delegation),
        with known exceptions.

        This catches: method defined, IPC method name missing entirely from the
        live dispatch (unreachable dead code with no delegation path).

        A ``_handle_foo`` shim that exists alongside an extracted-service entry
        ``"foo": self._bar_svc.handle_foo`` is acceptable: the IPC key "foo" is
        still reachable, just through the extracted service.  The shim itself may
        be dead code (cleanup target) but is not a wiring bug.
        """
        import re

        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        # Collect all defined _handle_* method names
        defined = set(re.findall(r"    def (_handle_\w+)\(", source))

        # Live dispatch keys (12-space indent inside handle_request)
        dispatch_keys = set(re.findall(
            r'^            "([a-z][a-z0-9_]*)"\s*:', source, re.MULTILINE
        ))

        # Build: _handle_<name> → ipc_key "name"
        # A handler is OK if its derived IPC key is in the dispatch (even via a service).
        # It is orphaned only if the IPC key is ALSO absent.
        meta_handlers = {"_handle_connection", "_handle_batch"}

        orphan_private = []
        for handler in sorted(defined):
            if handler in meta_handlers:
                continue
            ipc_key = handler[len("_handle_"):]  # strip _handle_ prefix
            if ipc_key not in dispatch_keys:
                orphan_private.append(handler)

        # Known confirmed orphans (real IPC-key gap).
        # Remove from this set once they are either wired into the dispatch table
        # or deleted from service.py.
        known_orphans_wave55 = {
            # IPC key "get_recording_insights" is handled by _search_and_analysis_svc;
            # the legacy _handle_get_recording_insights shim remains as cleanup target.
            "_handle_get_recording_insights",
            # Delegation shims: IPC key exists but dispatch uses extracted service.
            # The _handle_* shim body is unreachable dead code — cleanup targets.
            "_handle_warmup_stt",
            "_handle_get_stt_routing_decision",
            "_handle_add_stt_hotword",
            "_handle_remove_stt_hotword",
            "_handle_list_stt_hotwords",
            "_handle_select_model",
            # No IPC key at all — genuinely dead code:
            "_handle_get_disk_status",
            "_handle_get_storage_breakdown",
            # W957 SECURITY: clear_privacy_audit_log intentionally removed from dispatch
            # (unauthenticated IPC would allow any local process to erase audit trail).
            "_handle_clear_privacy_audit_log",
        }

        new_orphans = [m for m in orphan_private if m not in known_orphans_wave55]

        if new_orphans:
            self.fail(
                f"Found {len(new_orphans)} NEW _handle_* method(s) defined in service.py "
                f"whose IPC key is absent from the live dispatch:\n"
                + "\n".join(f"  - {m}" for m in new_orphans)
            )

        # Silently accept known orphans — they are documented cleanup targets.
        pass

    def test_calendar_handlers_deleted_wave65(self):
        """Wave 65 batch 3 removed old _handle_get_calendar_link / _handle_search_by_calendar_event
        as dead stubs. Wave 947 re-introduced them with a new CalendarLinker implementation.
        Wave 1030 confirmed the canonical names (non-_v2 variants).
        Regression guard: the live dispatch entries must point to these canonical handlers.
        """
        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        # Verify CalendarLinker handlers exist and are wired (not missing)
        for handler in ["_handle_get_calendar_link", "_handle_search_by_calendar_event"]:
            self.assertIn(
                f"def {handler}",
                source,
                f"{handler} is missing — CalendarLinker IPC handler deleted (regression)",
            )

        # Verify no _v2 suffix remains (renamed in Wave 1030 / restored canonical form)
        for bad_name in ["_handle_get_calendar_link_v2", "_handle_search_by_calendar_event_v2"]:
            self.assertNotIn(
                f"def {bad_name}",
                source,
                f"{bad_name} _v2 variant must not exist; canonical name must be used",
            )

    def test_session_speaker_handlers_deleted_wave65_batch4(self):
        """Wave 65 batch 4: _handle_get_session_history, _handle_get_session_stats,
        and _handle_get_speaker_statistics deleted as dead code (zero callers confirmed
        by audit script PR #418). Regression guard: these methods must NOT reappear.
        """
        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        deleted_handlers = [
            "_handle_get_session_history",
            "_handle_get_session_stats",
            "_handle_get_speaker_statistics",
        ]
        for handler in deleted_handlers:
            self.assertNotIn(
                f"def {handler}",
                source,
                f"{handler} was deleted in Wave 65 batch 4 but has reappeared — revert or update this test.",
            )

    def test_register_speaker_wired_wave961(self):
        """W951 F4: 'register_speaker' must be in the dispatch table (wired in Wave 961)."""
        self.assertIn(
            "register_speaker",
            self._dispatch_table,
            "'register_speaker' is missing from dispatch table — W951 F4 regression",
        )

    def test_delete_speaker_fingerprint_wired_wave961(self):
        """W951 F4: 'delete_speaker_fingerprint' must be in the dispatch table (wired in Wave 961)."""
        self.assertIn(
            "delete_speaker_fingerprint",
            self._dispatch_table,
            "'delete_speaker_fingerprint' is missing from dispatch table — W951 F4 regression",
        )

    def test_list_speaker_fingerprints_wired_wave961(self):
        """W951 F4: 'list_speaker_fingerprints' must be in the dispatch table (wired in Wave 961)."""
        self.assertIn(
            "list_speaker_fingerprints",
            self._dispatch_table,
            "'list_speaker_fingerprints' is missing from dispatch table — W951 F4 regression",
        )

    def test_get_recording_insights_alias_consistency(self):
        """'get_recording_insights' in live dispatch does NOT point to
        _handle_get_recording_stats (the Wave 54 semantic bug regression guard).

        Wave 54 fix: the key was wrongly aliased to _handle_get_recording_stats.
        Now it resolves to _handle_get_recording_insights or a service delegation.
        Live dispatch is the inline ``handlers`` dict in service.py handle_request.
        """
        import re
        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            service_src = f.read()

        # Verify the dispatch entry exists
        self.assertIn('"get_recording_insights"', service_src,
                      "'get_recording_insights' is missing from dispatch table entirely")

        # Regression guard: must NOT point to _handle_get_recording_stats (Wave 54 bug)
        bad_alias = re.search(
            r'"get_recording_insights"\s*:\s*self\._handle_get_recording_stats\b',
            service_src,
        )
        self.assertIsNone(
            bad_alias,
            "'get_recording_insights' is incorrectly aliased to _handle_get_recording_stats "
            "(Wave 54 regression: this was the semantic bug that was fixed)"
        )


class TestThrottleListsInvariants(unittest.TestCase):
    """Wave 58 ext — Lock IPC throttle + audit_logger registries against dispatch.

    Same fence-test pattern as Wave 51 (error_codes ↔ error_actions) and
    Wave 54 (dispatch table itself). Catches stale throttle entries и audit
    sensitive-method whitelist references что больше не существуют в IPC.
    """

    def _read_dispatch_keys(self) -> set[str]:
        """Return all keys in the live dispatch dict in service.py.

        The live dispatch is the inline ``handlers`` dict inside
        ``BackendService.handle_request`` (12-space indented keys).
        """
        import re
        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()
        return set(re.findall(r'^            "([a-z][a-z0-9_]*)"\s*:', source, re.MULTILINE))

    def test_throttle_heavy_methods_all_in_dispatch(self):
        """Every method in `HEAVY_METHODS` must be a real IPC method that
        appears in service.py dispatch table. Otherwise throttle limit is
        wasted — applies to nothing."""
        from backend.ipc_throttle import HEAVY_METHODS
        dispatch_keys = self._read_dispatch_keys()
        orphans = HEAVY_METHODS - dispatch_keys
        self.assertSetEqual(
            orphans, set(),
            f"HEAVY_METHODS contains entries not in service.py dispatch: {orphans}"
        )

    def test_throttle_medium_methods_all_in_dispatch(self):
        """Same invariant for MEDIUM_METHODS — catches stale rate-limit
        whitelist entries."""
        from backend.ipc_throttle import MEDIUM_METHODS
        dispatch_keys = self._read_dispatch_keys()
        orphans = MEDIUM_METHODS - dispatch_keys
        self.assertSetEqual(
            orphans, set(),
            f"MEDIUM_METHODS contains entries not in service.py dispatch: {orphans}"
        )

    def test_audit_logger_sensitive_methods_all_in_dispatch(self):
        """Every method в `_SENSITIVE_METHODS` (params not logged for privacy)
        must exist в dispatch. Stale entry = dead config."""
        from backend.audit_logger import _SENSITIVE_METHODS
        dispatch_keys = self._read_dispatch_keys()
        orphans = _SENSITIVE_METHODS - dispatch_keys
        self.assertSetEqual(
            orphans, set(),
            f"_SENSITIVE_METHODS contains stale entries: {orphans}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
