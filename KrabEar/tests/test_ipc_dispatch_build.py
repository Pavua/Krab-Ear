"""Unit tests for the LIVE IPC dispatch table (W887 → W1769).

W1769: dispatch table consolidated inline in ``backend/service.py`` as
``BackendService._build_dispatch_table`` (built once in ``__init__``, cached as
``self._dispatch_table``).  ``backend/ipc_dispatch.py`` (the drifted dead copy)
has been DELETED.  These tests now assert against the REAL runtime table.

Covers:
- Table is a non-empty dict with all-callable values
- Known anchor keys are present
- All values are callable
- All keys are plain strings
- Lambda entries (late-injection patterns) are callable
- _build_dispatch_table() returns a fresh dict each call
- Building with a None sub-service raises AttributeError (documents the contract)
- No duplicate keys in the service.py dispatch dict source
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

# ---------------------------------------------------------------------------
# Path setup — same pattern as other KrabEar test files
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)


# ---------------------------------------------------------------------------
# Helper: construct a REAL BackendService with light fakes (no heavy ML load).
# Same proven pattern as test_dispatch_complete — recorder/transcriber/translator
# are fakes injected via the constructor; AudioEngine is never instantiated
# because no transcription is performed.
# ---------------------------------------------------------------------------

def _build_minimal_backend_service():
    import numpy as np
    from backend.state_store import StateStore
    from backend.service import BackendService
    from backend.translator import TranslationResult

    class _FakeRecorder:
        is_recording = False
        sample_rate = 16000

        def start(self):
            self.is_recording = True
            return True

        def stop(self, timeout_sec=3.0, trim_tail_ms=0):
            if not self.is_recording:
                return None
            self.is_recording = False
            return np.zeros(16000, dtype=np.float32), 1.0

    class _FakeEngine:
        _last_llm_diff = None
        _llm_rewriter = None
        quality_profile = "balanced"
        current_model = "fake-model"

        def _resolve_diarization_device(self) -> str:
            return "cpu"

    class _FakeTranscriber:
        def __init__(self):
            self.engine = _FakeEngine()

        def transcribe(self, *a, **kw):
            return "fake"

    class _FakeTranslator:
        last_mode = "off"

        def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
            return TranslationResult(
                text="", status="not_requested", source_lang="",
                target_lang="", mode=mode, engine="fake",
            )

    tmp = tempfile.mkdtemp()
    store = StateStore(__import__("pathlib").Path(tmp) / "data")
    return BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildDispatchTableBasics(unittest.TestCase):
    """Structural tests for the LIVE BackendService._dispatch_table."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _build_minimal_backend_service()
        cls.table = cls.svc._dispatch_table

    # --- Test 1 ---
    def test_returns_non_empty_dict(self):
        """_dispatch_table must be a non-empty dict."""
        self.assertIsInstance(self.table, dict)
        self.assertGreater(
            len(self.table), 200,
            f"Dispatch table has only {len(self.table)} entries; expected >200",
        )

    # --- Test 2 ---
    def test_all_values_are_callable(self):
        """Every value in the dispatch table must be callable (no None, no string)."""
        non_callable = [k for k, v in self.table.items() if not callable(v)]
        self.assertEqual(non_callable, [], f"Non-callable entries found: {non_callable}")

    # --- Test 3 ---
    def test_all_keys_are_strings(self):
        """Every key must be a plain str — no integer or None keys."""
        bad_keys = [k for k in self.table if not isinstance(k, str)]
        self.assertEqual(bad_keys, [], f"Non-string keys: {bad_keys}")

    # --- Test 4 ---
    def test_anchor_keys_present(self):
        """A representative set of well-known method names must exist in the table."""
        anchor_methods = [
            "ping",
            "start_recording",
            "stop_recording",
            "get_settings",
            "set_settings",
            "get_history_page",
            "translate_text",
            "health_check",
            "handshake",
            "batch",
            "live_subs_ingest",
            # W1769: rollback_migration now LIVE (was only in the dead ipc_dispatch.py)
            "rollback_migration",
        ]
        missing = [m for m in anchor_methods if m not in self.table]
        self.assertEqual(missing, [], f"Anchor keys missing from dispatch table: {missing}")

    # --- Test 5 ---
    def test_lambda_entries_are_callable(self):
        """Lambda-wrapped entries (get_auto_backup_status, etc.) must be callable."""
        lambda_keys = [
            "get_auto_backup_status",
            "get_export_schedule_status",
            "list_auto_exports",
            "merge_recordings",
            "preview_merge",
        ]
        for key in lambda_keys:
            with self.subTest(key=key):
                self.assertIn(key, self.table, f"Lambda key '{key}' missing")
                self.assertTrue(callable(self.table[key]), f"'{key}' is not callable")

    # --- Test 6 ---
    def test_fresh_dict_each_call(self):
        """Two successive _build_dispatch_table() calls return independent dicts."""
        table2 = self.svc._build_dispatch_table()
        self.assertIsNot(
            self.table, table2,
            "_build_dispatch_table returned the same dict object on two calls",
        )

    # --- Test 7 ---
    def test_none_sub_svc_attr_raises_during_build(self):
        """_build_dispatch_table must raise AttributeError if a sub-svc attr is None.

        The method reads svc._<svc>.handle_xxx at build time for bound-method
        lookups; when a required sub-service is None, accessing None.handle_xxx
        raises AttributeError.  This documents the contract (sub-services are
        constructed before the table is built).
        """
        svc2 = _build_minimal_backend_service()
        svc2._history = None  # will cause AttributeError on .handle_get_history_page
        with self.assertRaises(AttributeError):
            svc2._build_dispatch_table()

    # --- Test 8 ---
    def test_no_duplicate_keys(self):
        """The dispatch dict source in service.py must have no duplicate keys.

        Python dicts silently overwrite duplicate keys; a count mismatch would
        indicate a duplicate registration that shadows an earlier handler.
        W1769: the dict literal lives inside BackendService._build_dispatch_table
        (8-space indentation for top-level dict entries).
        """
        import inspect
        import re
        from backend.service import BackendService

        source = inspect.getsource(BackendService._build_dispatch_table)
        # Top-level dict entries are indented 12 spaces inside the method's
        # ``return {`` block (method body 8 + dict 4).  Lambda-body dict literals
        # (e.g. {"exports": ...}) are deeper/inline on the same line, so anchoring
        # to start-of-line + exactly 12 spaces avoids false positives.
        keys_in_source = re.findall(r'^            "([a-z][a-z0-9_]*)"\s*:', source, re.MULTILINE)
        unique_keys_in_source = set(keys_in_source)
        duplicates = [k for k in unique_keys_in_source if keys_in_source.count(k) > 1]
        self.assertEqual(
            duplicates, [],
            f"Duplicate keys found in service.py dispatch dict source: {duplicates}",
        )
        # And the built table must contain all unique source keys
        self.assertEqual(
            len(self.table), len(unique_keys_in_source),
            "Table length doesn't match unique source keys — a key may be shadowed",
        )




class TestW1773StrandedHandlersWired(unittest.TestCase):
    """W1773: verify the three previously stranded handlers are now live in
    the real dispatch table built by BackendService.__init__.

    These handlers existed and had passing behavior tests, but were only
    registered in the deleted dead ipc_dispatch.py — never reachable in
    production.  W1773 adds them to the inline _build_dispatch_table dict.
    """

    @classmethod
    def setUpClass(cls):
        cls.svc = _build_minimal_backend_service()
        cls.table = cls.svc._dispatch_table

    def test_get_never_played_in_live_table(self):
        """'get_never_played' must be present in the live dispatch table and callable."""
        self.assertIn(
            "get_never_played", self.table,
            "W1773: 'get_never_played' stranded handler was not wired",
        )
        self.assertTrue(callable(self.table["get_never_played"]))

    def test_rename_collection_in_live_table(self):
        """'rename_collection' must route to CollectionManager.handle_rename_collection."""
        self.assertIn(
            "rename_collection", self.table,
            "W1773: 'rename_collection' stranded handler was not wired",
        )
        handler = self.table["rename_collection"]
        self.assertTrue(callable(handler))
        # Bound methods are re-created on every attribute access, so use __func__ + __self__
        # to assert both the underlying function and the bound instance match.
        from backend.collection_manager import CollectionManager
        self.assertIsInstance(self.svc._collections, CollectionManager)
        self.assertIs(handler.__self__, self.svc._collections)
        self.assertIs(handler.__func__, CollectionManager.handle_rename_collection)

    def test_semantic_search_reset_in_live_table(self):
        """'semantic_search_reset' must route to SearchAndAnalysisService.handle_semantic_search_reset."""
        self.assertIn(
            "semantic_search_reset", self.table,
            "W1773: 'semantic_search_reset' stranded handler was not wired",
        )
        handler = self.table["semantic_search_reset"]
        self.assertTrue(callable(handler))
        # Same bound-method identity pattern as above.
        from backend.search_and_analysis_service import SearchAndAnalysisService
        self.assertIsInstance(self.svc._search_and_analysis_svc, SearchAndAnalysisService)
        self.assertIs(handler.__self__, self.svc._search_and_analysis_svc)
        self.assertIs(handler.__func__, SearchAndAnalysisService.handle_semantic_search_reset)


if __name__ == "__main__":
    unittest.main()
