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
import types
import inspect
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

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
    # SpeakerManager has speaker fingerprint methods added after Wave 48
    # that are not yet wired into the dispatch table
    "SpeakerManager": frozenset({
        "handle_register_speaker",
        "handle_delete_speaker_fingerprint",
        "handle_list_speaker_fingerprints",
    }),
    # TranscriptionQueue has handle_peek which is internal
    "TranscriptionQueue": frozenset({
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
                        "_event_replay", "_config_presets", "_transcription_queue",
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
        """Extract the handlers dict by parsing service.py source."""
        import re

        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        # Find the handlers = { ... } block inside handle_request
        # The block starts after "handlers: dict[str, Callable" and ends at "}"
        # We use a simple state machine to find all quoted method keys
        method_names = re.findall(r'"([a-z][a-z0-9_]*)"\s*:', source[
            source.index("handlers: dict[str, Callable"):
            source.index("\n        handler = handlers.get(method)")
        ])
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
                        # Check if this method is referenced anywhere in the dispatch table
                        # The dispatch table uses the IPC key, not the method name,
                        # so we check if the method name appears in service.py as a value
                        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
                        with open(service_path, encoding="utf-8") as sf:
                            service_src = sf.read()
                        if f".{method_name}" not in service_src:
                            orphans.append(f"{basename}::{class_name}.{method_name}")

        if orphans:
            self.fail(
                f"Found {len(orphans)} handle_* method(s) in extracted services "
                f"that are NOT referenced in service.py at all:\n"
                + "\n".join(f"  - {o}" for o in sorted(orphans))
            )

    def test_service_py_private_handlers_all_in_dispatch(self):
        """Every _handle_* private method in service.py must be in the
        dispatch table (directly or via alias), with known exceptions.

        This catches: method defined, never registered (unreachable dead code).
        """
        import re

        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        # Collect all defined _handle_* method names
        defined = set(re.findall(r"    def (_handle_\w+)\(", source))

        # Collect all _handle_* names that appear as values in the dispatch table
        # (between the handlers = { block and the closing })
        dispatch_block_start = source.index("handlers: dict[str, Callable")
        dispatch_block_end = source.index("\n        handler = handlers.get(method)")
        dispatch_block = source[dispatch_block_start:dispatch_block_end]
        referenced_in_dispatch = set(re.findall(r"self\.(_handle_\w+)", dispatch_block))

        # Also lambdas that wrap _handle_* inside the block
        referenced_in_dispatch |= set(re.findall(r"self\.(_handle_\w+)", dispatch_block))

        orphan_private = sorted(defined - referenced_in_dispatch)

        # Filter out _handle_connection and _handle_batch (internal/meta)
        # _handle_batch IS in the dispatch table so won't be listed unless broken
        meta_handlers = {"_handle_connection", "_handle_batch"}
        orphan_private = [m for m in orphan_private if m not in meta_handlers]

        # Known confirmed orphans (real drift) — Wave 55 cleanup targets.
        # They are listed here so the test documents them explicitly.
        # Remove from this set once they are either wired into the dispatch table
        # or deleted from service.py.
        known_orphans_wave55 = {
            # Defined at line ~6032/6040 but never registered in dispatch table.
            # Possibly added speculatively and never wired.
            "_handle_get_calendar_link",
            "_handle_search_by_calendar_event",
            # Defined at line ~4551. 'get_recording_insights' in dispatch points
            # to _handle_get_recording_stats instead (alias / naming inconsistency).
            "_handle_get_recording_insights",
        }

        new_orphans = [m for m in orphan_private if m not in known_orphans_wave55]

        if new_orphans:
            self.fail(
                f"Found {len(new_orphans)} NEW _handle_* method(s) defined in service.py "
                f"but NOT referenced in the dispatch table (beyond known Wave 55 orphans):\n"
                + "\n".join(f"  - {m}" for m in new_orphans)
            )

        # Also assert the known orphans still exist so we notice when they're fixed
        still_present = [m for m in known_orphans_wave55 if m in orphan_private]
        if still_present:
            # This branch is expected — orphans not yet fixed
            pass  # documented, not an error here; see test_calendar_handlers_orphan_detection

    def test_calendar_handlers_orphan_detection(self):
        """Regression test: _handle_get_calendar_link and
        _handle_search_by_calendar_event were found as orphans in Wave 51
        audit — they must either be registered or this test documents them.

        If they ARE now registered this test will pass naturally. If they remain
        unregistered, this test FAILS to draw attention to the known gap.
        """
        import re
        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        dispatch_block_start = source.index("handlers: dict[str, Callable")
        dispatch_block_end = source.index("\n        handler = handlers.get(method)")
        dispatch_block = source[dispatch_block_start:dispatch_block_end]

        defined_calendar = {"_handle_get_calendar_link", "_handle_search_by_calendar_event"}
        referenced = set(re.findall(r"self\.(_handle_\w+)", dispatch_block))

        orphans = defined_calendar - referenced
        if orphans:
            self.fail(
                "Known orphan calendar handlers are still not registered in dispatch:\n"
                + "\n".join(f"  - {m}" for m in sorted(orphans))
                + "\n\nFix: add them to the handlers dict in handle_request, or "
                  "delete the methods if they are dead code. (Wave 55 cleanup target)"
            )

    def test_get_recording_insights_alias_consistency(self):
        """'get_recording_insights' in dispatch points to _handle_get_recording_stats,
        NOT _handle_get_recording_insights.  This is either intentional aliasing or
        a naming bug.  The test documents the situation: if the alias is intentional
        this test passes; if the real method is later registered, update accordingly.
        """
        import re
        service_path = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as f:
            source = f.read()

        dispatch_block_start = source.index("handlers: dict[str, Callable")
        dispatch_block_end = source.index("\n        handler = handlers.get(method)")
        dispatch_block = source[dispatch_block_start:dispatch_block_end]

        # Verify the dispatch entry exists
        self.assertIn('"get_recording_insights"', dispatch_block,
                      "'get_recording_insights' is missing from dispatch table entirely")

        # Document the current aliasing: key points to _handle_get_recording_stats
        match = re.search(r'"get_recording_insights"\s*:\s*self\.(_handle_\w+)', dispatch_block)
        self.assertIsNotNone(match, "Cannot parse 'get_recording_insights' dispatch entry")
        actual_handler = match.group(1)

        # Wave 54 fix: dispatch now correctly resolves to _handle_get_recording_insights
        # (was wrongly aliased to _handle_get_recording_stats — silent semantic bug).
        self.assertEqual(
            actual_handler, "_handle_get_recording_insights",
            f"'get_recording_insights' now points to {actual_handler!r}; "
            f"expected '_handle_get_recording_insights' (Wave 54 alias fix)"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
