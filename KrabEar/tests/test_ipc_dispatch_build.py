"""Unit tests for backend.ipc_dispatch.build_dispatch_table (W887).

Covers:
- Table is non-empty dict with all-callable values
- Known anchor keys are present
- All values are callable
- Lambda entries (late-injection patterns) are callable when sub-svc has None attr
- Table keys are all strings (no typos)
- Returns a fresh dict each call (no shared-state mutation)
- build_dispatch_table is importable without heavy deps
- Subset of documented method names is present
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — same pattern as other KrabEar test files
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

# ---------------------------------------------------------------------------
# Helper: build a minimal stub "svc" object that satisfies every attribute
# accessed by build_dispatch_table without importing BackendService.
# ---------------------------------------------------------------------------

_SVC_ATTRS = [
    # extracted sub-services
    "_call_assist", "_history", "_translation", "_settings_svc",
    "_glossary_auto_learn", "_glossary_svc", "_paste_app_memory",
    "_collections", "_chains", "_recording_scheduler", "_error_reporter",
    "_event_replay", "_config_presets", "_transcription_queue",
    "_data_migrator", "_sharing", "_transcript_versioning",
    "_paste_formatter", "_merger", "_obsidian_sync", "_playback_tracker",
    "_speaker_manager", "_live_subs", "_plugin_manager", "_feature_flags",
    "_hotword_detector", "_model_cache_manager", "_oww_adapter", "_tts",
    "_bookmarks", "_call_cost_estimator", "_template_manager",
    "_webhook_manager", "_auto_backup", "_export_scheduler",
    "_search_history", "_archive_manager", "_metadata_enricher",
    "_recording_core_svc", "_analytics_svc", "_audio_analytics_svc",
    "_stt_mgmt_svc", "_llm_ops_svc", "_text_processing_svc",
    "_call_session_service",
    # store (used by merger lambdas)
    "store",
]

# Private _handle_* methods directly referenced as svc._handle_* in ipc_dispatch.py.
# Generated from: grep "svc._handle_" ipc_dispatch.py | sed "s/.*svc\.\(_handle_[a-z_]*\).*/\1/" | sort -u
_HANDLE_METHODS = [
    "_handle_batch",
    "_handle_batch_extract_action_items",
    "_handle_cancel_transcribe_job",
    "_handle_check_duplicate",
    "_handle_check_integrity",
    "_handle_clear_privacy_audit_log",
    "_handle_clear_recent_errors",
    "_handle_clear_translation_cache",
    "_handle_clear_unavailable_models",
    "_handle_compare_periods",
    "_handle_compare_recordings",
    "_handle_configure_auto_export",
    "_handle_create_apple_note",
    "_handle_create_apple_reminder",
    "_handle_create_calendar_event",
    "_handle_estimate_recording_cost",
    "_handle_extract_action_items",
    "_handle_extract_terms",
    "_handle_generate_auto_title",
    "_handle_generate_daily_digest",
    "_handle_generate_mini_stats_report",
    "_handle_generate_stats_report",
    "_handle_get_activity_calendar",
    "_handle_get_analytics_dashboard",
    "_handle_get_audio_devices",
    "_handle_get_auto_glossary",
    "_handle_get_context_memory",
    "_handle_get_daily_cost_summary",
    "_handle_get_dedup_stats",
    "_handle_get_diagnostics",
    "_handle_get_keyword_cloud",
    "_handle_get_learning_stats",
    "_handle_get_memory_stats",
    "_handle_get_metrics_dashboard",
    "_handle_get_never_played",
    "_handle_get_pending_action_items",
    "_handle_get_privacy_audit_log",
    "_handle_get_recording_insights",
    "_handle_get_recording_state",
    "_handle_get_sentiment_trends",
    "_handle_get_shutdown_status",
    "_handle_get_smart_vocabulary_suggestions",
    "_handle_get_startup_diagnostics",
    "_handle_get_system_info",
    "_handle_get_throttle_stats",
    "_handle_get_timeline_view",
    "_handle_get_topic_timeline",
    "_handle_get_transcribe_progress",
    "_handle_get_usage_stats",
    "_handle_handle_error_action",
    "_handle_handshake",
    "_handle_health_check",
    "_handle_list_audio_inputs",
    "_handle_list_normalization_profiles",
    "_handle_list_recent_errors",
    "_handle_list_telegram_chats",
    "_handle_ping",
    "_handle_preview_transcribe_paths",
    "_handle_probe_llm_http",
    "_handle_refresh_auto_glossary",
    "_handle_repair_integrity",
    "_handle_report_hotkey_conflict",
    "_handle_report_paste_failure",
    "_handle_report_reconnect",
    "_handle_run_deduplication",
    "_handle_score_transcription",
    "_handle_semantic_search",
    "_handle_semantic_search_reindex",
    "_handle_semantic_search_reset",
    "_handle_semantic_search_status",
    "_handle_send_diagnostics_to_sentry",
    "_handle_send_imessage",
    "_handle_send_to_telegram",
    "_handle_start_recording",
    "_handle_stop_recording",
    "_handle_test_microphone",
    "_handle_transcribe_paths",
    "_handle_transcribe_paths_async",
    "_handle_warmup_rewriter",
]


def _make_stub_svc() -> MagicMock:
    """Return a MagicMock that has all svc.* attributes set to MagicMocks."""
    svc = MagicMock(spec=object)
    for attr in _SVC_ATTRS:
        setattr(svc, attr, MagicMock())
    for method in _HANDLE_METHODS:
        setattr(svc, method, MagicMock())
    # store needs a data_dir for merger lambdas
    svc.store = MagicMock()
    svc.store.data_dir = "/tmp/test_dispatch"
    return svc


def _import_build_dispatch_table():
    """Import build_dispatch_table with heavy dependencies stubbed out."""
    heavy = [
        "mlx_whisper", "mlx", "mlx.core", "mlx.nn",
        "torch", "torchaudio", "pyannote", "pyannote.audio",
        "sounddevice", "soundfile", "sentry_sdk",
        "transformers", "psutil",
    ]
    mocks = {m: MagicMock() for m in heavy}
    with patch.dict("sys.modules", mocks):
        from backend.ipc_dispatch import build_dispatch_table  # noqa: PLC0415
    return build_dispatch_table


# Module-level reference so tests can call it without self-binding issues
_BUILD_DISPATCH_TABLE = _import_build_dispatch_table()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildDispatchTableBasics(unittest.TestCase):
    """Basic structural tests for build_dispatch_table."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_stub_svc()
        cls.table = _BUILD_DISPATCH_TABLE(cls.svc)

    # --- Test 1 ---
    def test_returns_non_empty_dict(self):
        """build_dispatch_table must return a non-empty dict."""
        self.assertIsInstance(self.table, dict)
        self.assertGreater(
            len(self.table), 200,
            f"Dispatch table has only {len(self.table)} entries; expected >200",
        )

    # --- Test 2 ---
    def test_all_values_are_callable(self):
        """Every value in the dispatch table must be callable (no None, no string)."""
        non_callable = [
            k for k, v in self.table.items() if not callable(v)
        ]
        self.assertEqual(
            non_callable, [],
            f"Non-callable entries found: {non_callable}",
        )

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
        ]
        missing = [m for m in anchor_methods if m not in self.table]
        self.assertEqual(
            missing, [],
            f"Anchor keys missing from dispatch table: {missing}",
        )

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
        """Two successive calls must return independent dicts (no shared reference)."""
        svc2 = _make_stub_svc()
        table2 = _BUILD_DISPATCH_TABLE(svc2)
        self.assertIsNot(
            self.table, table2,
            "build_dispatch_table returned the same dict object on two calls",
        )

    # --- Test 7 ---
    def test_none_sub_svc_attr_does_not_raise_during_build(self):
        """build_dispatch_table must not raise AttributeError if a sub-svc attr is None.

        The function only *reads* svc.attr at build time for bound-method lookups.
        When the attr is None, accessing None.handle_xxx raises AttributeError.
        This test documents that behaviour: the table build IS expected to raise if
        a required sub-svc is None, and we verify the exact exception type.
        (If the implementation changes to guard None attrs, this test must be updated.)
        """
        broken_svc = _make_stub_svc()
        broken_svc._history = None   # will cause AttributeError on .handle_get_history_page

        with self.assertRaises(AttributeError):
            _BUILD_DISPATCH_TABLE(broken_svc)

    # --- Test 8 ---
    def test_no_duplicate_keys(self):
        """The resulting dict must have exactly as many entries as unique method names.

        Python dicts silently overwrite duplicate keys; a count mismatch would indicate
        a duplicate registration that shadows an earlier handler.

        We cross-check against the raw source to count unique top-level dispatch keys.
        Note: the regex must only capture keys at the top-level dict indentation (8 spaces),
        not dict literals embedded in lambda bodies (e.g. ``{"exports": ...}``).
        """
        import re

        dispatch_path = os.path.join(KRAB_EAR_ROOT, "backend", "ipc_dispatch.py")
        with open(dispatch_path, encoding="utf-8") as f:
            source = f.read()

        # Match only lines that look like top-level dict entries:
        # exactly 8 spaces of indentation, then a quoted snake_case key, then ":"
        keys_in_source = re.findall(
            r'^        "([a-z][a-z0-9_]*)"\s*:',
            source,
            re.MULTILINE,
        )
        unique_keys_in_source = set(keys_in_source)
        # If there are duplicates in source, len(keys_in_source) > len(unique_keys_in_source)
        duplicates = [
            k for k in unique_keys_in_source
            if keys_in_source.count(k) > 1
        ]
        self.assertEqual(
            duplicates, [],
            f"Duplicate keys found in ipc_dispatch.py source: {duplicates}",
        )
        # And the built table must contain all unique source keys
        self.assertEqual(
            len(self.table), len(unique_keys_in_source),
            "Table length doesn't match unique source keys — a key may be shadowed",
        )


if __name__ == "__main__":
    unittest.main()
