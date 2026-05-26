"""Dispatch invariant tests — Wave 790 (full coverage).

Covers all 263 IPC handler keys NOT yet asserted by the earlier
W654 / W693 / W768 test files.  Every test is a pure source-grep — no runtime
import of service.py is required.

After this file, combined coverage spans all 294 real dispatch keys
in ``BackendService.handle_request``.
"""

import os
import re
import sys
import unittest
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

SERVICE_PY = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")

# ---------------------------------------------------------------------------
# Expected dispatch-table entries (key -> RHS self.<svc>.handle_<method>).
# Sorted alphabetically for easy auditing.
# ---------------------------------------------------------------------------
_EXPECTED = {
    "add_bookmark": "self._bookmarks.handle_add_bookmark",
    "add_hotword": "self._hotword_detector.handle_add_hotword",
    "add_summary_profile": "self._history.handle_add_summary_profile",
    "add_tag": "self._history.handle_add_tag",
    "add_template": "self._template_manager.handle_add_template",
    "add_to_chain": "self._chains.handle_add_to_chain",
    "add_to_collection": "self._collections.handle_add_to_collection",
    "analyze_audio_quality": "self._audio_analytics_svc.handle_analyze_audio_quality",
    "analyze_quality_trends": "self._audio_analytics_svc.handle_analyze_quality_trends",
    "analyze_silence": "self._audio_analytics_svc.handle_analyze_silence",
    "analyze_word_timing": "self._audio_analytics_svc.handle_analyze_word_timing",
    "apply_config_preset": "self._config_presets.handle_apply_config_preset",
    "apply_glossary_suggestions": "self._glossary_auto_learn.handle_apply_glossary_suggestions",
    "apply_template": "self._template_manager.handle_apply_template",
    "archive_items": "self._archive_manager.handle_archive_items",
    "auto_summarize_batch": "self._history.handle_auto_summarize_batch",
    "backup_history": "self._history.handle_backup_history",
    "batch": "self._handle_batch",
    "batch_export": "self._history.handle_batch_export",
    "batch_extract_action_items": "self._handle_batch_extract_action_items",
    "call_assist_add_template": "self._call_assist.handle_add_template",
    "call_assist_cost_estimate": "self._call_assist.handle_cost_estimate",
    "call_assist_cost_report": "self._call_assist.handle_cost_report",
    "call_assist_diagnostics": "self._call_assist.handle_diagnostics",
    "call_assist_list_templates": "self._call_assist.handle_list_templates",
    "call_assist_quick_phrase": "self._call_assist.handle_quick_phrase",
    "call_assist_remove_template": "self._call_assist.handle_remove_template",
    "call_assist_summary": "self._call_assist.handle_summary",
    "call_assist_template": "self._call_assist.handle_template",
    "call_assist_timeline": "self._call_assist.handle_timeline",
    "call_assist_timeline_clear": "self._call_assist.handle_timeline_clear",
    "call_assist_timeline_export": "self._call_assist.handle_timeline_export",
    "call_assist_timeline_stats": "self._call_assist.handle_timeline_stats",
    "call_assist_timeline_summary": "self._call_assist.handle_timeline_summary",
    "call_assist_timeline_to_history": "self._call_assist.handle_timeline_to_history",
    "call_check_auto_end": "self._call_auto_end.handle_check_auto_end",
    "call_estimate_cost": "self._call_cost_estimator.handle_estimate_cost",
    "call_session_add_transcript": "self._call_session_service.handle_call_session_add_transcript",
    "call_session_create": "self._call_session_service.handle_call_session_create",
    "call_session_end": "self._call_session_service.handle_call_session_end",
    "call_session_get": "self._call_session_service.handle_call_session_get",
    "call_session_list": "self._call_session_service.handle_call_session_list",
    "call_session_update_status": "self._call_session_service.handle_call_session_update_status",
    "cancel_scheduled_recording": "self._recording_scheduler.handle_cancel_scheduled_recording",
    "cancel_transcribe_job": "self._handle_cancel_transcribe_job",
    "cancel_transcription": "self._transcription_queue.handle_cancel",
    "check_audio_duplicate": "self._audio_analytics_svc.handle_check_audio_duplicate",
    "check_duplicate": "self._handle_check_duplicate",
    "check_hotwords": "self._hotword_detector.handle_check_hotwords",
    "check_migration": "self._data_migrator.handle_check_migration",
    "cleanup_old_history": "self._history.handle_cleanup_old_history",
    "cleanup_stale_app_profiles": "self._paste_app_memory.handle_cleanup_stale_app_profiles",
    "clear_privacy_audit_log": "self._handle_clear_privacy_audit_log",
    "clear_recent_errors": "self._handle_clear_recent_errors",
    "clear_search_history": "self._search_history.handle_clear_search_history",
    "compact_history": "self._history.handle_compact_history",
    "compare_recordings": "self._handle_compare_recordings",
    "compare_texts": "self._text_processing_svc.handle_compare_texts",
    "configure_auto_export": "self._handle_configure_auto_export",
    "configure_obsidian_sync": "self._obsidian_sync.handle_configure",
    "create_apple_note": "self._handle_create_apple_note",
    "create_apple_reminder": "self._handle_create_apple_reminder",
    "create_calendar_event": "self._handle_create_calendar_event",
    "create_collection": "self._collections.handle_create_collection",
    "create_config_preset": "self._config_presets.handle_create_config_preset",
    "create_manual_settings_backup": "self._settings_svc.handle_create_manual_settings_backup",
    "delete_app_profile": "self._paste_app_memory.handle_delete_app_profile",
    "delete_bookmark": "self._bookmarks.handle_delete_bookmark",
    "delete_collection": "self._collections.handle_delete_collection",
    "detect_emotion": "self._text_processing_svc.handle_detect_emotion",
    "end_chain": "self._chains.handle_end_chain",
    "enqueue_transcription": "self._transcription_queue.handle_enqueue",
    "enrich_recording": "self._metadata_enricher.handle_enrich_recording",
    "estimate_recording_cost": "self._handle_estimate_recording_cost",
    "expand_abbreviations": "self._text_processing_svc.handle_expand_abbreviations",
    "export_history": "self._history.handle_export_history",
    "export_history_csv": "self._history.handle_export_history_csv",
    "export_history_json": "self._history.handle_export_history_json",
    "export_history_markdown": "self._history.handle_export_history_markdown",
    "export_html_report": "self._history.handle_export_html_report",
    "export_obsidian": "self._history.handle_export_obsidian",
    "export_settings": "self._settings_svc.handle_export_settings",
    "exports": "self._export_scheduler.list_exports(",
    "extract_terms": "self._handle_extract_terms",
    "filter_by_confidence": "self._history.handle_filter_by_confidence",
    "find_duplicates": "self._history.handle_find_duplicates",
    "format_for_paste": "self._paste_formatter.handle_format_for_paste",
    "fuzzy_search": "self._history.handle_fuzzy_search",
    "generate_auto_title": "self._handle_generate_auto_title",
    "generate_daily_digest": "self._handle_generate_daily_digest",
    "generate_html_report": "self._history.handle_export_html_report",
    "generate_mini_stats_report": "self._handle_generate_mini_stats_report",
    "generate_stats_report": "self._handle_generate_stats_report",
    "get_analytics_dashboard": "self._handle_get_analytics_dashboard",
    "get_annotation": "self._history.handle_get_annotation",
    "get_archive_stats": "self._archive_manager.handle_get_archive_stats",
    "get_audio_devices": "self._handle_get_audio_devices",
    "get_audio_info": "self._audio_analytics_svc.handle_get_audio_info",
    # "get_auto_backup_status" uses lambda (presence-only assertion below)
    "get_call_assist_state": "self._call_assist.handle_get_state",
    "get_chain": "self._chains.handle_get_chain",
    "get_clipboard_history": "self._history.handle_get_clipboard_history",
    "get_collection_items": "self._collections.handle_get_collection_items",
    "get_context_memory": "self._handle_get_context_memory",
    "get_daily_cost_summary": "self._handle_get_daily_cost_summary",
    "get_dedup_stats": "self._handle_get_dedup_stats",
    "get_error_report": "self._error_reporter.handle_get_error_report",
    "get_error_stats": "self._error_reporter.handle_get_error_stats",
    "get_event_log": "self._event_replay.handle_get_event_log",
    "get_event_stats": "self._event_replay.handle_get_event_stats",
    # "get_export_schedule_status" uses lambda (presence-only assertion below)
    "get_favorites": "self._history.handle_get_favorites",
    "get_feature_flags": "self._feature_flags.handle_get_feature_flags",
    "get_glossary_suggestions": "self._translation.handle_get_glossary_suggestions",
    "get_history_item": "self._history.handle_get_history_item",
    "get_history_overview": "self._history.handle_get_history_overview",
    "get_history_statistics": "self._history.handle_get_history_statistics",
    "get_history_stats": "self._history.handle_get_history_stats",
    "get_hotwords": "self._hotword_detector.handle_get_hotwords",
    "get_keyword_cloud": "self._handle_get_keyword_cloud",
    "get_learning_stats": "self._handle_get_learning_stats",
    "get_memory_stats": "self._handle_get_memory_stats",
    "get_model_cache_info": "self._model_cache_manager.handle_get_model_cache_info",
    "get_most_replayed": "self._playback_tracker.handle_get_most_replayed",
    "get_notification_preferences": "self._settings_svc.handle_get_notification_preferences",
    "get_obsidian_sync_status": "self._obsidian_sync.handle_get_status",
    "get_paste_profile_for_app": "self._paste_app_memory.handle_get_paste_profile_for_app",
    "get_pending_action_items": "self._handle_get_pending_action_items",
    "get_playback_stats": "self._playback_tracker.handle_get_playback_stats",
    "get_plugin_info": "self._plugin_manager.handle_get_plugin_info",
    "get_popular_searches": "self._search_history.handle_get_popular_searches",
    "get_privacy_audit_log": "self._handle_get_privacy_audit_log",
    "get_queue_status": "self._transcription_queue.handle_get_status",
    "get_recent_searches": "self._search_history.handle_get_recent_searches",
    "get_recording_insights": "self._handle_get_recording_insights",
    "get_recording_stats": "self._analytics_svc.handle_get_recording_stats",
    "get_sentiment_trends": "self._handle_get_sentiment_trends",
    "get_shared": "self._sharing.handle_get_shared",
    "get_shutdown_status": "self._handle_get_shutdown_status",
    "get_smart_vocabulary_suggestions": "self._handle_get_smart_vocabulary_suggestions",
    "get_speaker_aliases": "self._speaker_manager.handle_get_speaker_aliases",
    "get_startup_diagnostics": "self._handle_get_startup_diagnostics",
    "get_storage_info": "self._history.handle_get_storage_info",
    "get_system_info": "self._handle_get_system_info",
    "get_tags": "self._history.handle_get_tags",
    "get_templates": "self._template_manager.handle_get_templates",
    "get_throttle_stats": "self._handle_get_throttle_stats",
    "get_timeline_view": "self._handle_get_timeline_view",
    "get_topic_timeline": "self._handle_get_topic_timeline",
    "get_transcribe_progress": "self._handle_get_transcribe_progress",
    "get_transcript_versions": "self._transcript_versioning.handle_get_transcript_versions",
    "get_transcripts_path": "self._history.handle_get_transcripts_path",
    "get_usage_stats": "self._handle_get_usage_stats",
    "get_vocabulary_suggestions": "self._translation.handle_get_vocabulary_suggestions",
    "get_waveform": "self._audio_analytics_svc.handle_get_waveform",
    "handle_error_action": "self._handle_handle_error_action",
    "health_check": "self._handle_health_check",
    "import_glossary_csv": "self._glossary_svc.handle_import_glossary_csv",
    "import_history_ndjson": "self._history.handle_import_history_ndjson",
    "import_settings": "self._settings_svc.handle_import_settings",
    "is_favorite": "self._history.handle_is_favorite",
    "jump_to_bookmark": "self._bookmarks.handle_jump_to_bookmark",
    "list_abbreviations": "self._text_processing_svc.handle_list_abbreviations",
    "list_all_bookmarks": "self._bookmarks.handle_list_all_bookmarks",
    "list_all_tags": "self._history.handle_list_all_tags",
    "list_app_profiles": "self._paste_app_memory.handle_list_app_profiles",
    "list_archived": "self._archive_manager.handle_list_archived",
    # "list_auto_exports" uses lambda (presence-only assertion below)
    "list_backups": "self._history.handle_list_backups",
    "list_bookmarks": "self._bookmarks.handle_list_bookmarks",
    "list_cached_models": "self._model_cache_manager.handle_list_cached_models",
    "list_call_assist_quick_phrases": "self._call_assist.handle_list_quick_phrases",
    "list_chains": "self._chains.handle_list_chains",
    "list_collections": "self._collections.handle_list_collections",
    "list_config_presets": "self._config_presets.handle_list_config_presets",
    "list_llm_models": "self._handle_list_llm_models",
    "list_normalization_profiles": "self._handle_list_normalization_profiles",
    "list_paste_formatters": "self._paste_formatter.handle_list_paste_formatters",
    "list_plugins": "self._plugin_manager.handle_list_plugins",
    "list_post_process_steps": "self._text_processing_svc.handle_list_post_process_steps",
    "list_profile_presets": "self._settings_svc.handle_list_profile_presets",
    "list_recent_errors": "self._handle_list_recent_errors",
    "list_scheduled_recordings": "self._recording_scheduler.handle_list_scheduled_recordings",
    "list_settings_backups": "self._settings_svc.handle_list_settings_backups",
    "list_shared": "self._sharing.handle_list_shared",
    "list_summary_profiles": "self._history.handle_list_summary_profiles",
    "list_telegram_chats": "self._handle_list_telegram_chats",
    "list_transcription_queue": "self._transcription_queue.handle_list_queue",
    "list_webhooks": "self._webhook_manager.handle_list_webhooks",
    "live_subs_ingest": "self._live_subs.handle_ingest",
    "live_subs_stop": "self._live_subs.handle_stop",
    "merge_chain_text": "self._chains.handle_merge_chain_text",
    # "merge_recordings" uses lambda (presence-only assertion below)
    "post_process_text": "self._text_processing_svc.handle_post_process_text",
    "prepare_share": "self._sharing.handle_prepare_share",
    # "preview_merge" uses lambda (presence-only assertion below)
    "preview_transcribe_paths": "self._handle_preview_transcribe_paths",
    "profile_noise": "self._audio_analytics_svc.handle_profile_noise",
    "record_paste_app_profile": "self._paste_app_memory.handle_record_paste_app_profile",
    "record_playback": "self._playback_tracker.handle_record_playback",
    "register_webhook": "self._webhook_manager.handle_register_webhook",
    "remove_abbreviation": "self._text_processing_svc.handle_remove_abbreviation",
    "remove_from_collection": "self._collections.handle_remove_from_collection",
    "remove_hotword": "self._hotword_detector.handle_remove_hotword",
    "remove_speaker_alias": "self._speaker_manager.handle_remove_speaker_alias",
    "remove_tag": "self._history.handle_remove_tag",
    "remove_template": "self._template_manager.handle_remove_template",
    "remove_translation_glossary_item": "self._translation.handle_remove_translation_glossary_item",
    "repair_integrity": "self._handle_repair_integrity",
    "repaste_item": "self._history.handle_repaste_item",
    "replay_events": "self._event_replay.handle_replay_events",
    "report_hotkey_conflict": "self._handle_report_hotkey_conflict",
    "report_paste_failure": "self._handle_report_paste_failure",
    "report_reconnect": "self._handle_report_reconnect",
    "restore_history": "self._history.handle_restore_history",
    "restore_settings_backup": "self._settings_svc.handle_restore_settings_backup",
    "revert_transcript_version": "self._transcript_versioning.handle_revert_transcript_version",
    "revoke_share_link": "self._sharing.handle_revoke_share_link",
    "run_deduplication": "self._handle_run_deduplication",
    "run_migration": "self._data_migrator.handle_run_migration",
    "run_obsidian_sync": "self._obsidian_sync.handle_sync",
    "save_transcript_version": "self._transcript_versioning.handle_save_transcript_version",
    "schedule_recording": "self._recording_scheduler.handle_schedule_recording",
    "score_readability": "self._text_processing_svc.handle_score_readability",
    "search_annotations": "self._history.handle_search_annotations",
    "search_by_speaker": "self._history.handle_search_by_speaker",
    "search_by_tag": "self._history.handle_search_by_tag",
    "search_with_highlights": "self._history.handle_search_with_highlights",
    "select_model": "self._stt_mgmt_svc.handle_select_model",
    "semantic_search": "self._handle_semantic_search",
    "semantic_search_reindex": "self._handle_semantic_search_reindex",
    "semantic_search_status": "self._handle_semantic_search_status",
    "send_diagnostics_to_sentry": "self._handle_send_diagnostics_to_sentry",
    "send_imessage": "self._handle_send_imessage",
    "send_to_telegram": "self._handle_send_to_telegram",
    "set_annotation": "self._history.handle_set_annotation",
    "set_feature_flag": "self._feature_flags.handle_set_feature_flag",
    "set_notification_preferences": "self._settings_svc.handle_set_notification_preferences",
    "set_paste_status": "self._recording_core_svc.handle_set_paste_status",
    "set_speaker_alias": "self._speaker_manager.handle_set_speaker_alias",
    "set_translation_glossary_item": "self._translation.handle_set_translation_glossary_item",
    "start_call_assist": "self._call_assist.handle_start",
    "start_chain": "self._chains.handle_start_chain",
    "stop_call_assist": "self._call_assist.handle_stop",
    "suggest_medical_glossary_terms": "self._glossary_auto_learn.handle_suggest_medical_glossary_terms",
    "summarize_item": "self._text_processing_svc.handle_summarize_item",
    "summarize_text": "self._text_processing_svc.handle_summarize_text",
    "synthesize_speech": "self._tts.handle_synthesize_speech",
    "test_microphone": "self._handle_test_microphone",
    "toggle_favorite": "self._history.handle_toggle_favorite",
    "transcribe_paths": "self._handle_transcribe_paths",
    "transcribe_paths_async": "self._handle_transcribe_paths_async",
    "unarchive_items": "self._archive_manager.handle_unarchive_items",
    "unlink_recording_from_chain": "self._chains.handle_unlink_recording_from_chain",
    "unload_plugin": "self._plugin_manager.handle_unload_plugin",
    "unregister_webhook": "self._webhook_manager.handle_unregister_webhook",
    "wake_word_list_models": "self._oww_adapter.handle_wake_word_list_models",
    "wake_word_start": "self._oww_adapter.handle_wake_word_start",
    "wake_word_status": "self._oww_adapter.handle_wake_word_status",
    "wake_word_stop": "self._oww_adapter.handle_wake_word_stop",
    "warmup_rewriter": "self._handle_warmup_rewriter",
    "warmup_stt": "self._stt_mgmt_svc.handle_warmup_stt",
    "word_frequency_analysis": "self._history.handle_word_frequency_analysis",
}


def _read_source():
    with open(SERVICE_PY, encoding="utf-8") as f:
        return f.read()


def _dispatch_block(src):
    """Return the text of the handlers dict literal in handle_request."""
    start = src.index("handlers: dict[str, Callable")
    end = src.index("\n        handler = handlers.get(method)")
    return src[start:end]


def _all_dispatch_keys(block):
    return set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', block))


def _dispatch_rhs(block, key):
    """Return the RHS (self...) for key in the dispatch block, or None."""
    # Use non-raw string to avoid literal newline in character class
    newline = "\n"
    pattern = '"' + re.escape(key) + r'"\s*:\s*(self\.[^\s,#' + newline + r']+)'
    m = re.search(pattern, block)
    if m is None:
        return None
    return m.group(1).rstrip("}),")

class TestWave790FullDispatchCoverage(unittest.TestCase):
    """Wave 790 -- full dispatch invariants for 263 additional IPC handlers.

    Combined with W654 / W693 / W768 files, every handler in the
    BackendService.handle_request dispatch table now has at least one
    source-grep assertion.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source()
        cls.block = _dispatch_block(cls.src)
        cls.keys = _all_dispatch_keys(cls.block)

    # ------------------------------------------------------------------
    # Bulk presence test -- catches any handler silently removed at once
    # ------------------------------------------------------------------
    def test_all_wave790_handlers_registered(self):
        """All 263 Wave 790 IPC methods must exist in the dispatch table."""
        missing = set(_EXPECTED) - self.keys
        self.assertSetEqual(
            missing,
            set(),
            "IPC handler(s) missing from dispatch table: %s" % sorted(missing),
        )

    def test_all_wave790_rhs_correct(self):
        """Every Wave 790 handler must map to the expected delegate/implementation."""
        wrong = {
            k: (expected, _dispatch_rhs(self.block, k))
            for k, expected in _EXPECTED.items()
            if _dispatch_rhs(self.block, k) != expected
        }
        self.assertDictEqual(
            wrong,
            {},
            "Handler(s) with incorrect RHS mapping:\n" + "\n".join(
                "  %r: expected %r, got %r" % (k, exp, act)
                for k, (exp, act) in sorted(wrong.items())
            ),
        )

    # ------------------------------------------------------------------
    # Per-handler tests -- one per key for fine-grained failure attribution
    # ------------------------------------------------------------------

    def test_add_bookmark_dispatch_entry(self):
        """'add_bookmark' must be in dispatch table mapping to self._bookmarks.handle_add_bookmark."""
        self.assertIn("add_bookmark", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "add_bookmark"), "self._bookmarks.handle_add_bookmark")

    def test_add_hotword_dispatch_entry(self):
        """'add_hotword' must be in dispatch table mapping to self._hotword_detector.handle_add_hotword."""
        self.assertIn("add_hotword", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "add_hotword"), "self._hotword_detector.handle_add_hotword")

    def test_add_summary_profile_dispatch_entry(self):
        """'add_summary_profile' must be in dispatch table mapping to self._history.handle_add_summary_profile."""
        self.assertIn("add_summary_profile", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "add_summary_profile"), "self._history.handle_add_summary_profile")

    def test_add_tag_dispatch_entry(self):
        """'add_tag' must be in dispatch table mapping to self._history.handle_add_tag."""
        self.assertIn("add_tag", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "add_tag"), "self._history.handle_add_tag")

    def test_add_template_dispatch_entry(self):
        """'add_template' must be in dispatch table mapping to self._template_manager.handle_add_template."""
        self.assertIn("add_template", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "add_template"), "self._template_manager.handle_add_template")

    def test_add_to_chain_dispatch_entry(self):
        """'add_to_chain' must be in dispatch table mapping to self._chains.handle_add_to_chain."""
        self.assertIn("add_to_chain", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "add_to_chain"), "self._chains.handle_add_to_chain")

    def test_add_to_collection_dispatch_entry(self):
        """'add_to_collection' must be in dispatch table mapping to self._collections.handle_add_to_collection."""
        self.assertIn("add_to_collection", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "add_to_collection"), "self._collections.handle_add_to_collection")

    def test_analyze_audio_quality_dispatch_entry(self):
        """'analyze_audio_quality' must be in dispatch table mapping to self._audio_analytics_svc.handle_analyze_audio_quality."""
        self.assertIn("analyze_audio_quality", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "analyze_audio_quality"), "self._audio_analytics_svc.handle_analyze_audio_quality")

    def test_analyze_quality_trends_dispatch_entry(self):
        """'analyze_quality_trends' must be in dispatch table mapping to self._audio_analytics_svc.handle_analyze_quality_trends."""
        self.assertIn("analyze_quality_trends", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "analyze_quality_trends"), "self._audio_analytics_svc.handle_analyze_quality_trends")

    def test_analyze_silence_dispatch_entry(self):
        """'analyze_silence' must be in dispatch table mapping to self._audio_analytics_svc.handle_analyze_silence."""
        self.assertIn("analyze_silence", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "analyze_silence"), "self._audio_analytics_svc.handle_analyze_silence")

    def test_analyze_word_timing_dispatch_entry(self):
        """'analyze_word_timing' must be in dispatch table mapping to self._audio_analytics_svc.handle_analyze_word_timing."""
        self.assertIn("analyze_word_timing", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "analyze_word_timing"), "self._audio_analytics_svc.handle_analyze_word_timing")

    def test_apply_config_preset_dispatch_entry(self):
        """'apply_config_preset' must be in dispatch table mapping to self._config_presets.handle_apply_config_preset."""
        self.assertIn("apply_config_preset", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "apply_config_preset"), "self._config_presets.handle_apply_config_preset")

    def test_apply_glossary_suggestions_dispatch_entry(self):
        """'apply_glossary_suggestions' must be in dispatch table mapping to self._glossary_auto_learn.handle_apply_glossary_suggestions."""
        self.assertIn("apply_glossary_suggestions", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "apply_glossary_suggestions"), "self._glossary_auto_learn.handle_apply_glossary_suggestions")

    def test_apply_template_dispatch_entry(self):
        """'apply_template' must be in dispatch table mapping to self._template_manager.handle_apply_template."""
        self.assertIn("apply_template", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "apply_template"), "self._template_manager.handle_apply_template")

    def test_archive_items_dispatch_entry(self):
        """'archive_items' must be in dispatch table mapping to self._archive_manager.handle_archive_items."""
        self.assertIn("archive_items", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "archive_items"), "self._archive_manager.handle_archive_items")

    def test_auto_summarize_batch_dispatch_entry(self):
        """'auto_summarize_batch' must be in dispatch table mapping to self._history.handle_auto_summarize_batch."""
        self.assertIn("auto_summarize_batch", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "auto_summarize_batch"), "self._history.handle_auto_summarize_batch")

    def test_backup_history_dispatch_entry(self):
        """'backup_history' must be in dispatch table mapping to self._history.handle_backup_history."""
        self.assertIn("backup_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "backup_history"), "self._history.handle_backup_history")

    def test_batch_dispatch_entry(self):
        """'batch' must be in dispatch table mapping to self._handle_batch."""
        self.assertIn("batch", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "batch"), "self._handle_batch")

    def test_batch_export_dispatch_entry(self):
        """'batch_export' must be in dispatch table mapping to self._history.handle_batch_export."""
        self.assertIn("batch_export", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "batch_export"), "self._history.handle_batch_export")

    def test_batch_extract_action_items_dispatch_entry(self):
        """'batch_extract_action_items' must be in dispatch table mapping to self._handle_batch_extract_action_items."""
        self.assertIn("batch_extract_action_items", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "batch_extract_action_items"), "self._handle_batch_extract_action_items")

    def test_call_assist_add_template_dispatch_entry(self):
        """'call_assist_add_template' must be in dispatch table mapping to self._call_assist.handle_add_template."""
        self.assertIn("call_assist_add_template", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_add_template"), "self._call_assist.handle_add_template")

    def test_call_assist_cost_estimate_dispatch_entry(self):
        """'call_assist_cost_estimate' must be in dispatch table mapping to self._call_assist.handle_cost_estimate."""
        self.assertIn("call_assist_cost_estimate", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_cost_estimate"), "self._call_assist.handle_cost_estimate")

    def test_call_assist_cost_report_dispatch_entry(self):
        """'call_assist_cost_report' must be in dispatch table mapping to self._call_assist.handle_cost_report."""
        self.assertIn("call_assist_cost_report", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_cost_report"), "self._call_assist.handle_cost_report")

    def test_call_assist_diagnostics_dispatch_entry(self):
        """'call_assist_diagnostics' must be in dispatch table mapping to self._call_assist.handle_diagnostics."""
        self.assertIn("call_assist_diagnostics", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_diagnostics"), "self._call_assist.handle_diagnostics")

    def test_call_assist_list_templates_dispatch_entry(self):
        """'call_assist_list_templates' must be in dispatch table mapping to self._call_assist.handle_list_templates."""
        self.assertIn("call_assist_list_templates", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_list_templates"), "self._call_assist.handle_list_templates")

    def test_call_assist_quick_phrase_dispatch_entry(self):
        """'call_assist_quick_phrase' must be in dispatch table mapping to self._call_assist.handle_quick_phrase."""
        self.assertIn("call_assist_quick_phrase", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_quick_phrase"), "self._call_assist.handle_quick_phrase")

    def test_call_assist_remove_template_dispatch_entry(self):
        """'call_assist_remove_template' must be in dispatch table mapping to self._call_assist.handle_remove_template."""
        self.assertIn("call_assist_remove_template", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_remove_template"), "self._call_assist.handle_remove_template")

    def test_call_assist_summary_dispatch_entry(self):
        """'call_assist_summary' must be in dispatch table mapping to self._call_assist.handle_summary."""
        self.assertIn("call_assist_summary", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_summary"), "self._call_assist.handle_summary")

    def test_call_assist_template_dispatch_entry(self):
        """'call_assist_template' must be in dispatch table mapping to self._call_assist.handle_template."""
        self.assertIn("call_assist_template", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_template"), "self._call_assist.handle_template")

    def test_call_assist_timeline_dispatch_entry(self):
        """'call_assist_timeline' must be in dispatch table mapping to self._call_assist.handle_timeline."""
        self.assertIn("call_assist_timeline", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_timeline"), "self._call_assist.handle_timeline")

    def test_call_assist_timeline_clear_dispatch_entry(self):
        """'call_assist_timeline_clear' must be in dispatch table mapping to self._call_assist.handle_timeline_clear."""
        self.assertIn("call_assist_timeline_clear", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_timeline_clear"), "self._call_assist.handle_timeline_clear")

    def test_call_assist_timeline_export_dispatch_entry(self):
        """'call_assist_timeline_export' must be in dispatch table mapping to self._call_assist.handle_timeline_export."""
        self.assertIn("call_assist_timeline_export", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_timeline_export"), "self._call_assist.handle_timeline_export")

    def test_call_assist_timeline_stats_dispatch_entry(self):
        """'call_assist_timeline_stats' must be in dispatch table mapping to self._call_assist.handle_timeline_stats."""
        self.assertIn("call_assist_timeline_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_timeline_stats"), "self._call_assist.handle_timeline_stats")

    def test_call_assist_timeline_summary_dispatch_entry(self):
        """'call_assist_timeline_summary' must be in dispatch table mapping to self._call_assist.handle_timeline_summary."""
        self.assertIn("call_assist_timeline_summary", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_timeline_summary"), "self._call_assist.handle_timeline_summary")

    def test_call_assist_timeline_to_history_dispatch_entry(self):
        """'call_assist_timeline_to_history' must be in dispatch table mapping to self._call_assist.handle_timeline_to_history."""
        self.assertIn("call_assist_timeline_to_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_assist_timeline_to_history"), "self._call_assist.handle_timeline_to_history")

    def test_call_check_auto_end_dispatch_entry(self):
        """'call_check_auto_end' must be in dispatch table mapping to self._call_auto_end.handle_check_auto_end."""
        self.assertIn("call_check_auto_end", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_check_auto_end"), "self._call_auto_end.handle_check_auto_end")

    def test_call_estimate_cost_dispatch_entry(self):
        """'call_estimate_cost' must be in dispatch table mapping to self._call_cost_estimator.handle_estimate_cost."""
        self.assertIn("call_estimate_cost", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_estimate_cost"), "self._call_cost_estimator.handle_estimate_cost")

    def test_call_session_add_transcript_dispatch_entry(self):
        """'call_session_add_transcript' must be in dispatch table mapping to self._call_session_service.handle_call_session_add_transcript."""
        self.assertIn("call_session_add_transcript", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_session_add_transcript"), "self._call_session_service.handle_call_session_add_transcript")

    def test_call_session_create_dispatch_entry(self):
        """'call_session_create' must be in dispatch table mapping to self._call_session_service.handle_call_session_create."""
        self.assertIn("call_session_create", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_session_create"), "self._call_session_service.handle_call_session_create")

    def test_call_session_end_dispatch_entry(self):
        """'call_session_end' must be in dispatch table mapping to self._call_session_service.handle_call_session_end."""
        self.assertIn("call_session_end", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_session_end"), "self._call_session_service.handle_call_session_end")

    def test_call_session_get_dispatch_entry(self):
        """'call_session_get' must be in dispatch table mapping to self._call_session_service.handle_call_session_get."""
        self.assertIn("call_session_get", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_session_get"), "self._call_session_service.handle_call_session_get")

    def test_call_session_list_dispatch_entry(self):
        """'call_session_list' must be in dispatch table mapping to self._call_session_service.handle_call_session_list."""
        self.assertIn("call_session_list", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_session_list"), "self._call_session_service.handle_call_session_list")

    def test_call_session_update_status_dispatch_entry(self):
        """'call_session_update_status' must be in dispatch table mapping to self._call_session_service.handle_call_session_update_status."""
        self.assertIn("call_session_update_status", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "call_session_update_status"), "self._call_session_service.handle_call_session_update_status")

    def test_cancel_scheduled_recording_dispatch_entry(self):
        """'cancel_scheduled_recording' must be in dispatch table mapping to self._recording_scheduler.handle_cancel_scheduled_recording."""
        self.assertIn("cancel_scheduled_recording", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "cancel_scheduled_recording"), "self._recording_scheduler.handle_cancel_scheduled_recording")

    def test_cancel_transcribe_job_dispatch_entry(self):
        """'cancel_transcribe_job' must be in dispatch table mapping to self._handle_cancel_transcribe_job."""
        self.assertIn("cancel_transcribe_job", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "cancel_transcribe_job"), "self._handle_cancel_transcribe_job")

    def test_cancel_transcription_dispatch_entry(self):
        """'cancel_transcription' must be in dispatch table mapping to self._transcription_queue.handle_cancel."""
        self.assertIn("cancel_transcription", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "cancel_transcription"), "self._transcription_queue.handle_cancel")

    def test_check_audio_duplicate_dispatch_entry(self):
        """'check_audio_duplicate' must be in dispatch table mapping to self._audio_analytics_svc.handle_check_audio_duplicate."""
        self.assertIn("check_audio_duplicate", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "check_audio_duplicate"), "self._audio_analytics_svc.handle_check_audio_duplicate")

    def test_check_duplicate_dispatch_entry(self):
        """'check_duplicate' must be in dispatch table mapping to self._handle_check_duplicate."""
        self.assertIn("check_duplicate", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "check_duplicate"), "self._handle_check_duplicate")

    def test_check_hotwords_dispatch_entry(self):
        """'check_hotwords' must be in dispatch table mapping to self._hotword_detector.handle_check_hotwords."""
        self.assertIn("check_hotwords", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "check_hotwords"), "self._hotword_detector.handle_check_hotwords")

    def test_check_migration_dispatch_entry(self):
        """'check_migration' must be in dispatch table mapping to self._data_migrator.handle_check_migration."""
        self.assertIn("check_migration", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "check_migration"), "self._data_migrator.handle_check_migration")

    def test_cleanup_old_history_dispatch_entry(self):
        """'cleanup_old_history' must be in dispatch table mapping to self._history.handle_cleanup_old_history."""
        self.assertIn("cleanup_old_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "cleanup_old_history"), "self._history.handle_cleanup_old_history")

    def test_cleanup_stale_app_profiles_dispatch_entry(self):
        """'cleanup_stale_app_profiles' must be in dispatch table mapping to self._paste_app_memory.handle_cleanup_stale_app_profiles."""
        self.assertIn("cleanup_stale_app_profiles", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "cleanup_stale_app_profiles"), "self._paste_app_memory.handle_cleanup_stale_app_profiles")

    def test_clear_privacy_audit_log_dispatch_entry(self):
        """'clear_privacy_audit_log' must be in dispatch table mapping to self._handle_clear_privacy_audit_log."""
        self.assertIn("clear_privacy_audit_log", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "clear_privacy_audit_log"), "self._handle_clear_privacy_audit_log")

    def test_clear_recent_errors_dispatch_entry(self):
        """'clear_recent_errors' must be in dispatch table mapping to self._handle_clear_recent_errors."""
        self.assertIn("clear_recent_errors", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "clear_recent_errors"), "self._handle_clear_recent_errors")

    def test_clear_search_history_dispatch_entry(self):
        """'clear_search_history' must be in dispatch table mapping to self._search_history.handle_clear_search_history."""
        self.assertIn("clear_search_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "clear_search_history"), "self._search_history.handle_clear_search_history")

    def test_compact_history_dispatch_entry(self):
        """'compact_history' must be in dispatch table mapping to self._history.handle_compact_history."""
        self.assertIn("compact_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "compact_history"), "self._history.handle_compact_history")

    def test_compare_recordings_dispatch_entry(self):
        """'compare_recordings' must be in dispatch table mapping to self._handle_compare_recordings."""
        self.assertIn("compare_recordings", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "compare_recordings"), "self._handle_compare_recordings")

    def test_compare_texts_dispatch_entry(self):
        """'compare_texts' must be in dispatch table mapping to self._text_processing_svc.handle_compare_texts."""
        self.assertIn("compare_texts", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "compare_texts"), "self._text_processing_svc.handle_compare_texts")

    def test_configure_auto_export_dispatch_entry(self):
        """'configure_auto_export' must be in dispatch table mapping to self._handle_configure_auto_export."""
        self.assertIn("configure_auto_export", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "configure_auto_export"), "self._handle_configure_auto_export")

    def test_configure_obsidian_sync_dispatch_entry(self):
        """'configure_obsidian_sync' must be in dispatch table mapping to self._obsidian_sync.handle_configure."""
        self.assertIn("configure_obsidian_sync", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "configure_obsidian_sync"), "self._obsidian_sync.handle_configure")

    def test_create_apple_note_dispatch_entry(self):
        """'create_apple_note' must be in dispatch table mapping to self._handle_create_apple_note."""
        self.assertIn("create_apple_note", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "create_apple_note"), "self._handle_create_apple_note")

    def test_create_apple_reminder_dispatch_entry(self):
        """'create_apple_reminder' must be in dispatch table mapping to self._handle_create_apple_reminder."""
        self.assertIn("create_apple_reminder", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "create_apple_reminder"), "self._handle_create_apple_reminder")

    def test_create_calendar_event_dispatch_entry(self):
        """'create_calendar_event' must be in dispatch table mapping to self._handle_create_calendar_event."""
        self.assertIn("create_calendar_event", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "create_calendar_event"), "self._handle_create_calendar_event")

    def test_create_collection_dispatch_entry(self):
        """'create_collection' must be in dispatch table mapping to self._collections.handle_create_collection."""
        self.assertIn("create_collection", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "create_collection"), "self._collections.handle_create_collection")

    def test_create_config_preset_dispatch_entry(self):
        """'create_config_preset' must be in dispatch table mapping to self._config_presets.handle_create_config_preset."""
        self.assertIn("create_config_preset", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "create_config_preset"), "self._config_presets.handle_create_config_preset")

    def test_create_manual_settings_backup_dispatch_entry(self):
        """'create_manual_settings_backup' must be in dispatch table mapping to self._settings_svc.handle_create_manual_settings_backup."""
        self.assertIn("create_manual_settings_backup", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "create_manual_settings_backup"), "self._settings_svc.handle_create_manual_settings_backup")

    def test_delete_app_profile_dispatch_entry(self):
        """'delete_app_profile' must be in dispatch table mapping to self._paste_app_memory.handle_delete_app_profile."""
        self.assertIn("delete_app_profile", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "delete_app_profile"), "self._paste_app_memory.handle_delete_app_profile")

    def test_delete_bookmark_dispatch_entry(self):
        """'delete_bookmark' must be in dispatch table mapping to self._bookmarks.handle_delete_bookmark."""
        self.assertIn("delete_bookmark", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "delete_bookmark"), "self._bookmarks.handle_delete_bookmark")

    def test_delete_collection_dispatch_entry(self):
        """'delete_collection' must be in dispatch table mapping to self._collections.handle_delete_collection."""
        self.assertIn("delete_collection", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "delete_collection"), "self._collections.handle_delete_collection")

    def test_detect_emotion_dispatch_entry(self):
        """'detect_emotion' must be in dispatch table mapping to self._text_processing_svc.handle_detect_emotion."""
        self.assertIn("detect_emotion", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "detect_emotion"), "self._text_processing_svc.handle_detect_emotion")

    def test_end_chain_dispatch_entry(self):
        """'end_chain' must be in dispatch table mapping to self._chains.handle_end_chain."""
        self.assertIn("end_chain", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "end_chain"), "self._chains.handle_end_chain")

    def test_enqueue_transcription_dispatch_entry(self):
        """'enqueue_transcription' must be in dispatch table mapping to self._transcription_queue.handle_enqueue."""
        self.assertIn("enqueue_transcription", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "enqueue_transcription"), "self._transcription_queue.handle_enqueue")

    def test_enrich_recording_dispatch_entry(self):
        """'enrich_recording' must be in dispatch table mapping to self._metadata_enricher.handle_enrich_recording."""
        self.assertIn("enrich_recording", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "enrich_recording"), "self._metadata_enricher.handle_enrich_recording")

    def test_estimate_recording_cost_dispatch_entry(self):
        """'estimate_recording_cost' must be in dispatch table mapping to self._handle_estimate_recording_cost."""
        self.assertIn("estimate_recording_cost", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "estimate_recording_cost"), "self._handle_estimate_recording_cost")

    def test_expand_abbreviations_dispatch_entry(self):
        """'expand_abbreviations' must be in dispatch table mapping to self._text_processing_svc.handle_expand_abbreviations."""
        self.assertIn("expand_abbreviations", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "expand_abbreviations"), "self._text_processing_svc.handle_expand_abbreviations")

    def test_export_history_dispatch_entry(self):
        """'export_history' must be in dispatch table mapping to self._history.handle_export_history."""
        self.assertIn("export_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "export_history"), "self._history.handle_export_history")

    def test_export_history_csv_dispatch_entry(self):
        """'export_history_csv' must be in dispatch table mapping to self._history.handle_export_history_csv."""
        self.assertIn("export_history_csv", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "export_history_csv"), "self._history.handle_export_history_csv")

    def test_export_history_json_dispatch_entry(self):
        """'export_history_json' must be in dispatch table mapping to self._history.handle_export_history_json."""
        self.assertIn("export_history_json", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "export_history_json"), "self._history.handle_export_history_json")

    def test_export_history_markdown_dispatch_entry(self):
        """'export_history_markdown' must be in dispatch table mapping to self._history.handle_export_history_markdown."""
        self.assertIn("export_history_markdown", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "export_history_markdown"), "self._history.handle_export_history_markdown")

    def test_export_html_report_dispatch_entry(self):
        """'export_html_report' must be in dispatch table mapping to self._history.handle_export_html_report."""
        self.assertIn("export_html_report", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "export_html_report"), "self._history.handle_export_html_report")

    def test_export_obsidian_dispatch_entry(self):
        """'export_obsidian' must be in dispatch table mapping to self._history.handle_export_obsidian."""
        self.assertIn("export_obsidian", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "export_obsidian"), "self._history.handle_export_obsidian")

    def test_export_settings_dispatch_entry(self):
        """'export_settings' must be in dispatch table mapping to self._settings_svc.handle_export_settings."""
        self.assertIn("export_settings", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "export_settings"), "self._settings_svc.handle_export_settings")

    def test_exports_dispatch_entry(self):
        """'exports' must be in dispatch table mapping to self._export_scheduler.list_exports(."""
        self.assertIn("exports", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "exports"), "self._export_scheduler.list_exports(")

    def test_extract_terms_dispatch_entry(self):
        """'extract_terms' must be in dispatch table mapping to self._handle_extract_terms."""
        self.assertIn("extract_terms", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "extract_terms"), "self._handle_extract_terms")

    def test_filter_by_confidence_dispatch_entry(self):
        """'filter_by_confidence' must be in dispatch table mapping to self._history.handle_filter_by_confidence."""
        self.assertIn("filter_by_confidence", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "filter_by_confidence"), "self._history.handle_filter_by_confidence")

    def test_find_duplicates_dispatch_entry(self):
        """'find_duplicates' must be in dispatch table mapping to self._history.handle_find_duplicates."""
        self.assertIn("find_duplicates", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "find_duplicates"), "self._history.handle_find_duplicates")

    def test_format_for_paste_dispatch_entry(self):
        """'format_for_paste' must be in dispatch table mapping to self._paste_formatter.handle_format_for_paste."""
        self.assertIn("format_for_paste", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "format_for_paste"), "self._paste_formatter.handle_format_for_paste")

    def test_fuzzy_search_dispatch_entry(self):
        """'fuzzy_search' must be in dispatch table mapping to self._history.handle_fuzzy_search."""
        self.assertIn("fuzzy_search", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "fuzzy_search"), "self._history.handle_fuzzy_search")

    def test_generate_auto_title_dispatch_entry(self):
        """'generate_auto_title' must be in dispatch table mapping to self._handle_generate_auto_title."""
        self.assertIn("generate_auto_title", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "generate_auto_title"), "self._handle_generate_auto_title")

    def test_generate_daily_digest_dispatch_entry(self):
        """'generate_daily_digest' must be in dispatch table mapping to self._handle_generate_daily_digest."""
        self.assertIn("generate_daily_digest", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "generate_daily_digest"), "self._handle_generate_daily_digest")

    def test_generate_html_report_dispatch_entry(self):
        """'generate_html_report' must be in dispatch table mapping to self._history.handle_export_html_report."""
        self.assertIn("generate_html_report", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "generate_html_report"), "self._history.handle_export_html_report")

    def test_generate_mini_stats_report_dispatch_entry(self):
        """'generate_mini_stats_report' must be in dispatch table mapping to self._handle_generate_mini_stats_report."""
        self.assertIn("generate_mini_stats_report", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "generate_mini_stats_report"), "self._handle_generate_mini_stats_report")

    def test_generate_stats_report_dispatch_entry(self):
        """'generate_stats_report' must be in dispatch table mapping to self._handle_generate_stats_report."""
        self.assertIn("generate_stats_report", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "generate_stats_report"), "self._handle_generate_stats_report")

    def test_get_analytics_dashboard_dispatch_entry(self):
        """'get_analytics_dashboard' must be in dispatch table mapping to self._handle_get_analytics_dashboard."""
        self.assertIn("get_analytics_dashboard", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_analytics_dashboard"), "self._handle_get_analytics_dashboard")

    def test_get_annotation_dispatch_entry(self):
        """'get_annotation' must be in dispatch table mapping to self._history.handle_get_annotation."""
        self.assertIn("get_annotation", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_annotation"), "self._history.handle_get_annotation")

    def test_get_archive_stats_dispatch_entry(self):
        """'get_archive_stats' must be in dispatch table mapping to self._archive_manager.handle_get_archive_stats."""
        self.assertIn("get_archive_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_archive_stats"), "self._archive_manager.handle_get_archive_stats")

    def test_get_audio_devices_dispatch_entry(self):
        """'get_audio_devices' must be in dispatch table mapping to self._handle_get_audio_devices."""
        self.assertIn("get_audio_devices", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_audio_devices"), "self._handle_get_audio_devices")

    def test_get_audio_info_dispatch_entry(self):
        """'get_audio_info' must be in dispatch table mapping to self._audio_analytics_svc.handle_get_audio_info."""
        self.assertIn("get_audio_info", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_audio_info"), "self._audio_analytics_svc.handle_get_audio_info")

    def test_get_auto_backup_status_dispatch_entry(self):
        """'get_auto_backup_status' is registered in dispatch table (lambda-based RHS)."""
        # This handler uses a lambda, so only presence is asserted.
        self.assertIn("get_auto_backup_status", self.keys)

    def test_get_call_assist_state_dispatch_entry(self):
        """'get_call_assist_state' must be in dispatch table mapping to self._call_assist.handle_get_state."""
        self.assertIn("get_call_assist_state", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_call_assist_state"), "self._call_assist.handle_get_state")

    def test_get_chain_dispatch_entry(self):
        """'get_chain' must be in dispatch table mapping to self._chains.handle_get_chain."""
        self.assertIn("get_chain", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_chain"), "self._chains.handle_get_chain")

    def test_get_clipboard_history_dispatch_entry(self):
        """'get_clipboard_history' must be in dispatch table mapping to self._history.handle_get_clipboard_history."""
        self.assertIn("get_clipboard_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_clipboard_history"), "self._history.handle_get_clipboard_history")

    def test_get_collection_items_dispatch_entry(self):
        """'get_collection_items' must be in dispatch table mapping to self._collections.handle_get_collection_items."""
        self.assertIn("get_collection_items", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_collection_items"), "self._collections.handle_get_collection_items")

    def test_get_context_memory_dispatch_entry(self):
        """'get_context_memory' must be in dispatch table mapping to self._handle_get_context_memory."""
        self.assertIn("get_context_memory", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_context_memory"), "self._handle_get_context_memory")

    def test_get_daily_cost_summary_dispatch_entry(self):
        """'get_daily_cost_summary' must be in dispatch table mapping to self._handle_get_daily_cost_summary."""
        self.assertIn("get_daily_cost_summary", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_daily_cost_summary"), "self._handle_get_daily_cost_summary")

    def test_get_dedup_stats_dispatch_entry(self):
        """'get_dedup_stats' must be in dispatch table mapping to self._handle_get_dedup_stats."""
        self.assertIn("get_dedup_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_dedup_stats"), "self._handle_get_dedup_stats")

    def test_get_error_report_dispatch_entry(self):
        """'get_error_report' must be in dispatch table mapping to self._error_reporter.handle_get_error_report."""
        self.assertIn("get_error_report", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_error_report"), "self._error_reporter.handle_get_error_report")

    def test_get_error_stats_dispatch_entry(self):
        """'get_error_stats' must be in dispatch table mapping to self._error_reporter.handle_get_error_stats."""
        self.assertIn("get_error_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_error_stats"), "self._error_reporter.handle_get_error_stats")

    def test_get_event_log_dispatch_entry(self):
        """'get_event_log' must be in dispatch table mapping to self._event_replay.handle_get_event_log."""
        self.assertIn("get_event_log", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_event_log"), "self._event_replay.handle_get_event_log")

    def test_get_event_stats_dispatch_entry(self):
        """'get_event_stats' must be in dispatch table mapping to self._event_replay.handle_get_event_stats."""
        self.assertIn("get_event_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_event_stats"), "self._event_replay.handle_get_event_stats")

    def test_get_export_schedule_status_dispatch_entry(self):
        """'get_export_schedule_status' is registered in dispatch table (lambda-based RHS)."""
        # This handler uses a lambda, so only presence is asserted.
        self.assertIn("get_export_schedule_status", self.keys)

    def test_get_favorites_dispatch_entry(self):
        """'get_favorites' must be in dispatch table mapping to self._history.handle_get_favorites."""
        self.assertIn("get_favorites", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_favorites"), "self._history.handle_get_favorites")

    def test_get_feature_flags_dispatch_entry(self):
        """'get_feature_flags' must be in dispatch table mapping to self._feature_flags.handle_get_feature_flags."""
        self.assertIn("get_feature_flags", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_feature_flags"), "self._feature_flags.handle_get_feature_flags")

    def test_get_glossary_suggestions_dispatch_entry(self):
        """'get_glossary_suggestions' must be in dispatch table mapping to self._translation.handle_get_glossary_suggestions."""
        self.assertIn("get_glossary_suggestions", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_glossary_suggestions"), "self._translation.handle_get_glossary_suggestions")

    def test_get_history_item_dispatch_entry(self):
        """'get_history_item' must be in dispatch table mapping to self._history.handle_get_history_item."""
        self.assertIn("get_history_item", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_history_item"), "self._history.handle_get_history_item")

    def test_get_history_overview_dispatch_entry(self):
        """'get_history_overview' must be in dispatch table mapping to self._history.handle_get_history_overview."""
        self.assertIn("get_history_overview", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_history_overview"), "self._history.handle_get_history_overview")

    def test_get_history_statistics_dispatch_entry(self):
        """'get_history_statistics' must be in dispatch table mapping to self._history.handle_get_history_statistics."""
        self.assertIn("get_history_statistics", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_history_statistics"), "self._history.handle_get_history_statistics")

    def test_get_history_stats_dispatch_entry(self):
        """'get_history_stats' must be in dispatch table mapping to self._history.handle_get_history_stats."""
        self.assertIn("get_history_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_history_stats"), "self._history.handle_get_history_stats")

    def test_get_hotwords_dispatch_entry(self):
        """'get_hotwords' must be in dispatch table mapping to self._hotword_detector.handle_get_hotwords."""
        self.assertIn("get_hotwords", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_hotwords"), "self._hotword_detector.handle_get_hotwords")

    def test_get_keyword_cloud_dispatch_entry(self):
        """'get_keyword_cloud' must be in dispatch table mapping to self._handle_get_keyword_cloud."""
        self.assertIn("get_keyword_cloud", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_keyword_cloud"), "self._handle_get_keyword_cloud")

    def test_get_learning_stats_dispatch_entry(self):
        """'get_learning_stats' must be in dispatch table mapping to self._handle_get_learning_stats."""
        self.assertIn("get_learning_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_learning_stats"), "self._handle_get_learning_stats")

    def test_get_memory_stats_dispatch_entry(self):
        """'get_memory_stats' must be in dispatch table mapping to self._handle_get_memory_stats."""
        self.assertIn("get_memory_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_memory_stats"), "self._handle_get_memory_stats")

    def test_get_model_cache_info_dispatch_entry(self):
        """'get_model_cache_info' must be in dispatch table mapping to self._model_cache_manager.handle_get_model_cache_info."""
        self.assertIn("get_model_cache_info", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_model_cache_info"), "self._model_cache_manager.handle_get_model_cache_info")

    def test_get_most_replayed_dispatch_entry(self):
        """'get_most_replayed' must be in dispatch table mapping to self._playback_tracker.handle_get_most_replayed."""
        self.assertIn("get_most_replayed", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_most_replayed"), "self._playback_tracker.handle_get_most_replayed")

    def test_get_notification_preferences_dispatch_entry(self):
        """'get_notification_preferences' must be in dispatch table mapping to self._settings_svc.handle_get_notification_preferences."""
        self.assertIn("get_notification_preferences", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_notification_preferences"), "self._settings_svc.handle_get_notification_preferences")

    def test_get_obsidian_sync_status_dispatch_entry(self):
        """'get_obsidian_sync_status' must be in dispatch table mapping to self._obsidian_sync.handle_get_status."""
        self.assertIn("get_obsidian_sync_status", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_obsidian_sync_status"), "self._obsidian_sync.handle_get_status")

    def test_get_paste_profile_for_app_dispatch_entry(self):
        """'get_paste_profile_for_app' must be in dispatch table mapping to self._paste_app_memory.handle_get_paste_profile_for_app."""
        self.assertIn("get_paste_profile_for_app", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_paste_profile_for_app"), "self._paste_app_memory.handle_get_paste_profile_for_app")

    def test_get_pending_action_items_dispatch_entry(self):
        """'get_pending_action_items' must be in dispatch table mapping to self._handle_get_pending_action_items."""
        self.assertIn("get_pending_action_items", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_pending_action_items"), "self._handle_get_pending_action_items")

    def test_get_playback_stats_dispatch_entry(self):
        """'get_playback_stats' must be in dispatch table mapping to self._playback_tracker.handle_get_playback_stats."""
        self.assertIn("get_playback_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_playback_stats"), "self._playback_tracker.handle_get_playback_stats")

    def test_get_plugin_info_dispatch_entry(self):
        """'get_plugin_info' must be in dispatch table mapping to self._plugin_manager.handle_get_plugin_info."""
        self.assertIn("get_plugin_info", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_plugin_info"), "self._plugin_manager.handle_get_plugin_info")

    def test_get_popular_searches_dispatch_entry(self):
        """'get_popular_searches' must be in dispatch table mapping to self._search_history.handle_get_popular_searches."""
        self.assertIn("get_popular_searches", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_popular_searches"), "self._search_history.handle_get_popular_searches")

    def test_get_privacy_audit_log_dispatch_entry(self):
        """'get_privacy_audit_log' must be in dispatch table mapping to self._handle_get_privacy_audit_log."""
        self.assertIn("get_privacy_audit_log", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_privacy_audit_log"), "self._handle_get_privacy_audit_log")

    def test_get_queue_status_dispatch_entry(self):
        """'get_queue_status' must be in dispatch table mapping to self._transcription_queue.handle_get_status."""
        self.assertIn("get_queue_status", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_queue_status"), "self._transcription_queue.handle_get_status")

    def test_get_recent_searches_dispatch_entry(self):
        """'get_recent_searches' must be in dispatch table mapping to self._search_history.handle_get_recent_searches."""
        self.assertIn("get_recent_searches", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_recent_searches"), "self._search_history.handle_get_recent_searches")

    def test_get_recording_insights_dispatch_entry(self):
        """'get_recording_insights' must be in dispatch table mapping to self._handle_get_recording_insights."""
        self.assertIn("get_recording_insights", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_recording_insights"), "self._handle_get_recording_insights")

    def test_get_recording_stats_dispatch_entry(self):
        """'get_recording_stats' must be in dispatch table mapping to self._analytics_svc.handle_get_recording_stats."""
        self.assertIn("get_recording_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_recording_stats"), "self._analytics_svc.handle_get_recording_stats")

    def test_get_sentiment_trends_dispatch_entry(self):
        """'get_sentiment_trends' must be in dispatch table mapping to self._handle_get_sentiment_trends."""
        self.assertIn("get_sentiment_trends", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_sentiment_trends"), "self._handle_get_sentiment_trends")

    def test_get_shared_dispatch_entry(self):
        """'get_shared' must be in dispatch table mapping to self._sharing.handle_get_shared."""
        self.assertIn("get_shared", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_shared"), "self._sharing.handle_get_shared")

    def test_get_shutdown_status_dispatch_entry(self):
        """'get_shutdown_status' must be in dispatch table mapping to self._handle_get_shutdown_status."""
        self.assertIn("get_shutdown_status", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_shutdown_status"), "self._handle_get_shutdown_status")

    def test_get_smart_vocabulary_suggestions_dispatch_entry(self):
        """'get_smart_vocabulary_suggestions' must be in dispatch table mapping to self._handle_get_smart_vocabulary_suggestions."""
        self.assertIn("get_smart_vocabulary_suggestions", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_smart_vocabulary_suggestions"), "self._handle_get_smart_vocabulary_suggestions")

    def test_get_speaker_aliases_dispatch_entry(self):
        """'get_speaker_aliases' must be in dispatch table mapping to self._speaker_manager.handle_get_speaker_aliases."""
        self.assertIn("get_speaker_aliases", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_speaker_aliases"), "self._speaker_manager.handle_get_speaker_aliases")

    def test_get_startup_diagnostics_dispatch_entry(self):
        """'get_startup_diagnostics' must be in dispatch table mapping to self._handle_get_startup_diagnostics."""
        self.assertIn("get_startup_diagnostics", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_startup_diagnostics"), "self._handle_get_startup_diagnostics")

    def test_get_storage_info_dispatch_entry(self):
        """'get_storage_info' must be in dispatch table mapping to self._history.handle_get_storage_info."""
        self.assertIn("get_storage_info", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_storage_info"), "self._history.handle_get_storage_info")

    def test_get_system_info_dispatch_entry(self):
        """'get_system_info' must be in dispatch table mapping to self._handle_get_system_info."""
        self.assertIn("get_system_info", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_system_info"), "self._handle_get_system_info")

    def test_get_tags_dispatch_entry(self):
        """'get_tags' must be in dispatch table mapping to self._history.handle_get_tags."""
        self.assertIn("get_tags", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_tags"), "self._history.handle_get_tags")

    def test_get_templates_dispatch_entry(self):
        """'get_templates' must be in dispatch table mapping to self._template_manager.handle_get_templates."""
        self.assertIn("get_templates", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_templates"), "self._template_manager.handle_get_templates")

    def test_get_throttle_stats_dispatch_entry(self):
        """'get_throttle_stats' must be in dispatch table mapping to self._handle_get_throttle_stats."""
        self.assertIn("get_throttle_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_throttle_stats"), "self._handle_get_throttle_stats")

    def test_get_timeline_view_dispatch_entry(self):
        """'get_timeline_view' must be in dispatch table mapping to self._handle_get_timeline_view."""
        self.assertIn("get_timeline_view", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_timeline_view"), "self._handle_get_timeline_view")

    def test_get_topic_timeline_dispatch_entry(self):
        """'get_topic_timeline' must be in dispatch table mapping to self._handle_get_topic_timeline."""
        self.assertIn("get_topic_timeline", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_topic_timeline"), "self._handle_get_topic_timeline")

    def test_get_transcribe_progress_dispatch_entry(self):
        """'get_transcribe_progress' must be in dispatch table mapping to self._handle_get_transcribe_progress."""
        self.assertIn("get_transcribe_progress", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_transcribe_progress"), "self._handle_get_transcribe_progress")

    def test_get_transcript_versions_dispatch_entry(self):
        """'get_transcript_versions' must be in dispatch table mapping to self._transcript_versioning.handle_get_transcript_versions."""
        self.assertIn("get_transcript_versions", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_transcript_versions"), "self._transcript_versioning.handle_get_transcript_versions")

    def test_get_transcripts_path_dispatch_entry(self):
        """'get_transcripts_path' must be in dispatch table mapping to self._history.handle_get_transcripts_path."""
        self.assertIn("get_transcripts_path", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_transcripts_path"), "self._history.handle_get_transcripts_path")

    def test_get_usage_stats_dispatch_entry(self):
        """'get_usage_stats' must be in dispatch table mapping to self._handle_get_usage_stats."""
        self.assertIn("get_usage_stats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_usage_stats"), "self._handle_get_usage_stats")

    def test_get_vocabulary_suggestions_dispatch_entry(self):
        """'get_vocabulary_suggestions' must be in dispatch table mapping to self._translation.handle_get_vocabulary_suggestions."""
        self.assertIn("get_vocabulary_suggestions", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_vocabulary_suggestions"), "self._translation.handle_get_vocabulary_suggestions")

    def test_get_waveform_dispatch_entry(self):
        """'get_waveform' must be in dispatch table mapping to self._audio_analytics_svc.handle_get_waveform."""
        self.assertIn("get_waveform", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "get_waveform"), "self._audio_analytics_svc.handle_get_waveform")

    def test_handle_error_action_dispatch_entry(self):
        """'handle_error_action' must be in dispatch table mapping to self._handle_handle_error_action."""
        self.assertIn("handle_error_action", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "handle_error_action"), "self._handle_handle_error_action")

    def test_health_check_dispatch_entry(self):
        """'health_check' must be in dispatch table mapping to self._handle_health_check."""
        self.assertIn("health_check", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "health_check"), "self._handle_health_check")

    def test_import_glossary_csv_dispatch_entry(self):
        """'import_glossary_csv' must be in dispatch table mapping to self._glossary_svc.handle_import_glossary_csv."""
        self.assertIn("import_glossary_csv", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "import_glossary_csv"), "self._glossary_svc.handle_import_glossary_csv")

    def test_import_history_ndjson_dispatch_entry(self):
        """'import_history_ndjson' must be in dispatch table mapping to self._history.handle_import_history_ndjson."""
        self.assertIn("import_history_ndjson", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "import_history_ndjson"), "self._history.handle_import_history_ndjson")

    def test_import_settings_dispatch_entry(self):
        """'import_settings' must be in dispatch table mapping to self._settings_svc.handle_import_settings."""
        self.assertIn("import_settings", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "import_settings"), "self._settings_svc.handle_import_settings")

    def test_is_favorite_dispatch_entry(self):
        """'is_favorite' must be in dispatch table mapping to self._history.handle_is_favorite."""
        self.assertIn("is_favorite", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "is_favorite"), "self._history.handle_is_favorite")

    def test_jump_to_bookmark_dispatch_entry(self):
        """'jump_to_bookmark' must be in dispatch table mapping to self._bookmarks.handle_jump_to_bookmark."""
        self.assertIn("jump_to_bookmark", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "jump_to_bookmark"), "self._bookmarks.handle_jump_to_bookmark")

    def test_list_abbreviations_dispatch_entry(self):
        """'list_abbreviations' must be in dispatch table mapping to self._text_processing_svc.handle_list_abbreviations."""
        self.assertIn("list_abbreviations", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_abbreviations"), "self._text_processing_svc.handle_list_abbreviations")

    def test_list_all_bookmarks_dispatch_entry(self):
        """'list_all_bookmarks' must be in dispatch table mapping to self._bookmarks.handle_list_all_bookmarks."""
        self.assertIn("list_all_bookmarks", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_all_bookmarks"), "self._bookmarks.handle_list_all_bookmarks")

    def test_list_all_tags_dispatch_entry(self):
        """'list_all_tags' must be in dispatch table mapping to self._history.handle_list_all_tags."""
        self.assertIn("list_all_tags", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_all_tags"), "self._history.handle_list_all_tags")

    def test_list_app_profiles_dispatch_entry(self):
        """'list_app_profiles' must be in dispatch table mapping to self._paste_app_memory.handle_list_app_profiles."""
        self.assertIn("list_app_profiles", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_app_profiles"), "self._paste_app_memory.handle_list_app_profiles")

    def test_list_archived_dispatch_entry(self):
        """'list_archived' must be in dispatch table mapping to self._archive_manager.handle_list_archived."""
        self.assertIn("list_archived", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_archived"), "self._archive_manager.handle_list_archived")

    def test_list_auto_exports_dispatch_entry(self):
        """'list_auto_exports' is registered in dispatch table (lambda-based RHS)."""
        # This handler uses a lambda, so only presence is asserted.
        self.assertIn("list_auto_exports", self.keys)

    def test_list_backups_dispatch_entry(self):
        """'list_backups' must be in dispatch table mapping to self._history.handle_list_backups."""
        self.assertIn("list_backups", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_backups"), "self._history.handle_list_backups")

    def test_list_bookmarks_dispatch_entry(self):
        """'list_bookmarks' must be in dispatch table mapping to self._bookmarks.handle_list_bookmarks."""
        self.assertIn("list_bookmarks", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_bookmarks"), "self._bookmarks.handle_list_bookmarks")

    def test_list_cached_models_dispatch_entry(self):
        """'list_cached_models' must be in dispatch table mapping to self._model_cache_manager.handle_list_cached_models."""
        self.assertIn("list_cached_models", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_cached_models"), "self._model_cache_manager.handle_list_cached_models")

    def test_list_call_assist_quick_phrases_dispatch_entry(self):
        """'list_call_assist_quick_phrases' must be in dispatch table mapping to self._call_assist.handle_list_quick_phrases."""
        self.assertIn("list_call_assist_quick_phrases", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_call_assist_quick_phrases"), "self._call_assist.handle_list_quick_phrases")

    def test_list_chains_dispatch_entry(self):
        """'list_chains' must be in dispatch table mapping to self._chains.handle_list_chains."""
        self.assertIn("list_chains", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_chains"), "self._chains.handle_list_chains")

    def test_list_collections_dispatch_entry(self):
        """'list_collections' must be in dispatch table mapping to self._collections.handle_list_collections."""
        self.assertIn("list_collections", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_collections"), "self._collections.handle_list_collections")

    def test_list_config_presets_dispatch_entry(self):
        """'list_config_presets' must be in dispatch table mapping to self._config_presets.handle_list_config_presets."""
        self.assertIn("list_config_presets", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_config_presets"), "self._config_presets.handle_list_config_presets")

    def test_list_llm_models_dispatch_entry(self):
        """'list_llm_models' must be in dispatch table mapping to self._handle_list_llm_models."""
        self.assertIn("list_llm_models", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_llm_models"), "self._handle_list_llm_models")

    def test_list_normalization_profiles_dispatch_entry(self):
        """'list_normalization_profiles' must be in dispatch table mapping to self._handle_list_normalization_profiles."""
        self.assertIn("list_normalization_profiles", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_normalization_profiles"), "self._handle_list_normalization_profiles")

    def test_list_paste_formatters_dispatch_entry(self):
        """'list_paste_formatters' must be in dispatch table mapping to self._paste_formatter.handle_list_paste_formatters."""
        self.assertIn("list_paste_formatters", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_paste_formatters"), "self._paste_formatter.handle_list_paste_formatters")

    def test_list_plugins_dispatch_entry(self):
        """'list_plugins' must be in dispatch table mapping to self._plugin_manager.handle_list_plugins."""
        self.assertIn("list_plugins", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_plugins"), "self._plugin_manager.handle_list_plugins")

    def test_list_post_process_steps_dispatch_entry(self):
        """'list_post_process_steps' must be in dispatch table mapping to self._text_processing_svc.handle_list_post_process_steps."""
        self.assertIn("list_post_process_steps", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_post_process_steps"), "self._text_processing_svc.handle_list_post_process_steps")

    def test_list_profile_presets_dispatch_entry(self):
        """'list_profile_presets' must be in dispatch table mapping to self._settings_svc.handle_list_profile_presets."""
        self.assertIn("list_profile_presets", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_profile_presets"), "self._settings_svc.handle_list_profile_presets")

    def test_list_recent_errors_dispatch_entry(self):
        """'list_recent_errors' must be in dispatch table mapping to self._handle_list_recent_errors."""
        self.assertIn("list_recent_errors", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_recent_errors"), "self._handle_list_recent_errors")

    def test_list_scheduled_recordings_dispatch_entry(self):
        """'list_scheduled_recordings' must be in dispatch table mapping to self._recording_scheduler.handle_list_scheduled_recordings."""
        self.assertIn("list_scheduled_recordings", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_scheduled_recordings"), "self._recording_scheduler.handle_list_scheduled_recordings")

    def test_list_settings_backups_dispatch_entry(self):
        """'list_settings_backups' must be in dispatch table mapping to self._settings_svc.handle_list_settings_backups."""
        self.assertIn("list_settings_backups", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_settings_backups"), "self._settings_svc.handle_list_settings_backups")

    def test_list_shared_dispatch_entry(self):
        """'list_shared' must be in dispatch table mapping to self._sharing.handle_list_shared."""
        self.assertIn("list_shared", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_shared"), "self._sharing.handle_list_shared")

    def test_list_summary_profiles_dispatch_entry(self):
        """'list_summary_profiles' must be in dispatch table mapping to self._history.handle_list_summary_profiles."""
        self.assertIn("list_summary_profiles", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_summary_profiles"), "self._history.handle_list_summary_profiles")

    def test_list_telegram_chats_dispatch_entry(self):
        """'list_telegram_chats' must be in dispatch table mapping to self._handle_list_telegram_chats."""
        self.assertIn("list_telegram_chats", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_telegram_chats"), "self._handle_list_telegram_chats")

    def test_list_transcription_queue_dispatch_entry(self):
        """'list_transcription_queue' must be in dispatch table mapping to self._transcription_queue.handle_list_queue."""
        self.assertIn("list_transcription_queue", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_transcription_queue"), "self._transcription_queue.handle_list_queue")

    def test_list_webhooks_dispatch_entry(self):
        """'list_webhooks' must be in dispatch table mapping to self._webhook_manager.handle_list_webhooks."""
        self.assertIn("list_webhooks", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "list_webhooks"), "self._webhook_manager.handle_list_webhooks")

    def test_live_subs_ingest_dispatch_entry(self):
        """'live_subs_ingest' must be in dispatch table mapping to self._live_subs.handle_ingest."""
        self.assertIn("live_subs_ingest", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "live_subs_ingest"), "self._live_subs.handle_ingest")

    def test_live_subs_stop_dispatch_entry(self):
        """'live_subs_stop' must be in dispatch table mapping to self._live_subs.handle_stop."""
        self.assertIn("live_subs_stop", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "live_subs_stop"), "self._live_subs.handle_stop")

    def test_merge_chain_text_dispatch_entry(self):
        """'merge_chain_text' must be in dispatch table mapping to self._chains.handle_merge_chain_text."""
        self.assertIn("merge_chain_text", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "merge_chain_text"), "self._chains.handle_merge_chain_text")

    def test_merge_recordings_dispatch_entry(self):
        """'merge_recordings' is registered in dispatch table (lambda-based RHS)."""
        # This handler uses a lambda, so only presence is asserted.
        self.assertIn("merge_recordings", self.keys)

    def test_post_process_text_dispatch_entry(self):
        """'post_process_text' must be in dispatch table mapping to self._text_processing_svc.handle_post_process_text."""
        self.assertIn("post_process_text", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "post_process_text"), "self._text_processing_svc.handle_post_process_text")

    def test_prepare_share_dispatch_entry(self):
        """'prepare_share' must be in dispatch table mapping to self._sharing.handle_prepare_share."""
        self.assertIn("prepare_share", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "prepare_share"), "self._sharing.handle_prepare_share")

    def test_preview_merge_dispatch_entry(self):
        """'preview_merge' is registered in dispatch table (lambda-based RHS)."""
        # This handler uses a lambda, so only presence is asserted.
        self.assertIn("preview_merge", self.keys)

    def test_preview_transcribe_paths_dispatch_entry(self):
        """'preview_transcribe_paths' must be in dispatch table mapping to self._handle_preview_transcribe_paths."""
        self.assertIn("preview_transcribe_paths", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "preview_transcribe_paths"), "self._handle_preview_transcribe_paths")

    def test_profile_noise_dispatch_entry(self):
        """'profile_noise' must be in dispatch table mapping to self._audio_analytics_svc.handle_profile_noise."""
        self.assertIn("profile_noise", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "profile_noise"), "self._audio_analytics_svc.handle_profile_noise")

    def test_record_paste_app_profile_dispatch_entry(self):
        """'record_paste_app_profile' must be in dispatch table mapping to self._paste_app_memory.handle_record_paste_app_profile."""
        self.assertIn("record_paste_app_profile", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "record_paste_app_profile"), "self._paste_app_memory.handle_record_paste_app_profile")

    def test_record_playback_dispatch_entry(self):
        """'record_playback' must be in dispatch table mapping to self._playback_tracker.handle_record_playback."""
        self.assertIn("record_playback", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "record_playback"), "self._playback_tracker.handle_record_playback")

    def test_register_webhook_dispatch_entry(self):
        """'register_webhook' must be in dispatch table mapping to self._webhook_manager.handle_register_webhook."""
        self.assertIn("register_webhook", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "register_webhook"), "self._webhook_manager.handle_register_webhook")

    def test_remove_abbreviation_dispatch_entry(self):
        """'remove_abbreviation' must be in dispatch table mapping to self._text_processing_svc.handle_remove_abbreviation."""
        self.assertIn("remove_abbreviation", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "remove_abbreviation"), "self._text_processing_svc.handle_remove_abbreviation")

    def test_remove_from_collection_dispatch_entry(self):
        """'remove_from_collection' must be in dispatch table mapping to self._collections.handle_remove_from_collection."""
        self.assertIn("remove_from_collection", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "remove_from_collection"), "self._collections.handle_remove_from_collection")

    def test_remove_hotword_dispatch_entry(self):
        """'remove_hotword' must be in dispatch table mapping to self._hotword_detector.handle_remove_hotword."""
        self.assertIn("remove_hotword", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "remove_hotword"), "self._hotword_detector.handle_remove_hotword")

    def test_remove_speaker_alias_dispatch_entry(self):
        """'remove_speaker_alias' must be in dispatch table mapping to self._speaker_manager.handle_remove_speaker_alias."""
        self.assertIn("remove_speaker_alias", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "remove_speaker_alias"), "self._speaker_manager.handle_remove_speaker_alias")

    def test_remove_tag_dispatch_entry(self):
        """'remove_tag' must be in dispatch table mapping to self._history.handle_remove_tag."""
        self.assertIn("remove_tag", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "remove_tag"), "self._history.handle_remove_tag")

    def test_remove_template_dispatch_entry(self):
        """'remove_template' must be in dispatch table mapping to self._template_manager.handle_remove_template."""
        self.assertIn("remove_template", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "remove_template"), "self._template_manager.handle_remove_template")

    def test_remove_translation_glossary_item_dispatch_entry(self):
        """'remove_translation_glossary_item' must be in dispatch table mapping to self._translation.handle_remove_translation_glossary_item."""
        self.assertIn("remove_translation_glossary_item", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "remove_translation_glossary_item"), "self._translation.handle_remove_translation_glossary_item")

    def test_repair_integrity_dispatch_entry(self):
        """'repair_integrity' must be in dispatch table mapping to self._handle_repair_integrity."""
        self.assertIn("repair_integrity", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "repair_integrity"), "self._handle_repair_integrity")

    def test_repaste_item_dispatch_entry(self):
        """'repaste_item' must be in dispatch table mapping to self._history.handle_repaste_item."""
        self.assertIn("repaste_item", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "repaste_item"), "self._history.handle_repaste_item")

    def test_replay_events_dispatch_entry(self):
        """'replay_events' must be in dispatch table mapping to self._event_replay.handle_replay_events."""
        self.assertIn("replay_events", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "replay_events"), "self._event_replay.handle_replay_events")

    def test_report_hotkey_conflict_dispatch_entry(self):
        """'report_hotkey_conflict' must be in dispatch table mapping to self._handle_report_hotkey_conflict."""
        self.assertIn("report_hotkey_conflict", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "report_hotkey_conflict"), "self._handle_report_hotkey_conflict")

    def test_report_paste_failure_dispatch_entry(self):
        """'report_paste_failure' must be in dispatch table mapping to self._handle_report_paste_failure."""
        self.assertIn("report_paste_failure", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "report_paste_failure"), "self._handle_report_paste_failure")

    def test_report_reconnect_dispatch_entry(self):
        """'report_reconnect' must be in dispatch table mapping to self._handle_report_reconnect."""
        self.assertIn("report_reconnect", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "report_reconnect"), "self._handle_report_reconnect")

    def test_restore_history_dispatch_entry(self):
        """'restore_history' must be in dispatch table mapping to self._history.handle_restore_history."""
        self.assertIn("restore_history", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "restore_history"), "self._history.handle_restore_history")

    def test_restore_settings_backup_dispatch_entry(self):
        """'restore_settings_backup' must be in dispatch table mapping to self._settings_svc.handle_restore_settings_backup."""
        self.assertIn("restore_settings_backup", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "restore_settings_backup"), "self._settings_svc.handle_restore_settings_backup")

    def test_revert_transcript_version_dispatch_entry(self):
        """'revert_transcript_version' must be in dispatch table mapping to self._transcript_versioning.handle_revert_transcript_version."""
        self.assertIn("revert_transcript_version", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "revert_transcript_version"), "self._transcript_versioning.handle_revert_transcript_version")

    def test_revoke_share_link_dispatch_entry(self):
        """'revoke_share_link' must be in dispatch table mapping to self._sharing.handle_revoke_share_link."""
        self.assertIn("revoke_share_link", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "revoke_share_link"), "self._sharing.handle_revoke_share_link")

    def test_run_deduplication_dispatch_entry(self):
        """'run_deduplication' must be in dispatch table mapping to self._handle_run_deduplication."""
        self.assertIn("run_deduplication", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "run_deduplication"), "self._handle_run_deduplication")

    def test_run_migration_dispatch_entry(self):
        """'run_migration' must be in dispatch table mapping to self._data_migrator.handle_run_migration."""
        self.assertIn("run_migration", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "run_migration"), "self._data_migrator.handle_run_migration")

    def test_run_obsidian_sync_dispatch_entry(self):
        """'run_obsidian_sync' must be in dispatch table mapping to self._obsidian_sync.handle_sync."""
        self.assertIn("run_obsidian_sync", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "run_obsidian_sync"), "self._obsidian_sync.handle_sync")

    def test_save_transcript_version_dispatch_entry(self):
        """'save_transcript_version' must be in dispatch table mapping to self._transcript_versioning.handle_save_transcript_version."""
        self.assertIn("save_transcript_version", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "save_transcript_version"), "self._transcript_versioning.handle_save_transcript_version")

    def test_schedule_recording_dispatch_entry(self):
        """'schedule_recording' must be in dispatch table mapping to self._recording_scheduler.handle_schedule_recording."""
        self.assertIn("schedule_recording", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "schedule_recording"), "self._recording_scheduler.handle_schedule_recording")

    def test_score_readability_dispatch_entry(self):
        """'score_readability' must be in dispatch table mapping to self._text_processing_svc.handle_score_readability."""
        self.assertIn("score_readability", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "score_readability"), "self._text_processing_svc.handle_score_readability")

    def test_search_annotations_dispatch_entry(self):
        """'search_annotations' must be in dispatch table mapping to self._history.handle_search_annotations."""
        self.assertIn("search_annotations", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "search_annotations"), "self._history.handle_search_annotations")

    def test_search_by_speaker_dispatch_entry(self):
        """'search_by_speaker' must be in dispatch table mapping to self._history.handle_search_by_speaker."""
        self.assertIn("search_by_speaker", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "search_by_speaker"), "self._history.handle_search_by_speaker")

    def test_search_by_tag_dispatch_entry(self):
        """'search_by_tag' must be in dispatch table mapping to self._history.handle_search_by_tag."""
        self.assertIn("search_by_tag", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "search_by_tag"), "self._history.handle_search_by_tag")

    def test_search_with_highlights_dispatch_entry(self):
        """'search_with_highlights' must be in dispatch table mapping to self._history.handle_search_with_highlights."""
        self.assertIn("search_with_highlights", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "search_with_highlights"), "self._history.handle_search_with_highlights")

    def test_select_model_dispatch_entry(self):
        """'select_model' must be in dispatch table mapping to self._stt_mgmt_svc.handle_select_model."""
        self.assertIn("select_model", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "select_model"), "self._stt_mgmt_svc.handle_select_model")

    def test_semantic_search_dispatch_entry(self):
        """'semantic_search' must be in dispatch table mapping to self._handle_semantic_search."""
        self.assertIn("semantic_search", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "semantic_search"), "self._handle_semantic_search")

    def test_semantic_search_reindex_dispatch_entry(self):
        """'semantic_search_reindex' must be in dispatch table mapping to self._handle_semantic_search_reindex."""
        self.assertIn("semantic_search_reindex", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "semantic_search_reindex"), "self._handle_semantic_search_reindex")

    def test_semantic_search_status_dispatch_entry(self):
        """'semantic_search_status' must be in dispatch table mapping to self._handle_semantic_search_status."""
        self.assertIn("semantic_search_status", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "semantic_search_status"), "self._handle_semantic_search_status")

    def test_send_diagnostics_to_sentry_dispatch_entry(self):
        """'send_diagnostics_to_sentry' must be in dispatch table mapping to self._handle_send_diagnostics_to_sentry."""
        self.assertIn("send_diagnostics_to_sentry", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "send_diagnostics_to_sentry"), "self._handle_send_diagnostics_to_sentry")

    def test_send_imessage_dispatch_entry(self):
        """'send_imessage' must be in dispatch table mapping to self._handle_send_imessage."""
        self.assertIn("send_imessage", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "send_imessage"), "self._handle_send_imessage")

    def test_send_to_telegram_dispatch_entry(self):
        """'send_to_telegram' must be in dispatch table mapping to self._handle_send_to_telegram."""
        self.assertIn("send_to_telegram", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "send_to_telegram"), "self._handle_send_to_telegram")

    def test_set_annotation_dispatch_entry(self):
        """'set_annotation' must be in dispatch table mapping to self._history.handle_set_annotation."""
        self.assertIn("set_annotation", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "set_annotation"), "self._history.handle_set_annotation")

    def test_set_feature_flag_dispatch_entry(self):
        """'set_feature_flag' must be in dispatch table mapping to self._feature_flags.handle_set_feature_flag."""
        self.assertIn("set_feature_flag", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "set_feature_flag"), "self._feature_flags.handle_set_feature_flag")

    def test_set_notification_preferences_dispatch_entry(self):
        """'set_notification_preferences' must be in dispatch table mapping to self._settings_svc.handle_set_notification_preferences."""
        self.assertIn("set_notification_preferences", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "set_notification_preferences"), "self._settings_svc.handle_set_notification_preferences")

    def test_set_paste_status_dispatch_entry(self):
        """'set_paste_status' must be in dispatch table mapping to self._recording_core_svc.handle_set_paste_status."""
        self.assertIn("set_paste_status", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "set_paste_status"), "self._recording_core_svc.handle_set_paste_status")

    def test_set_speaker_alias_dispatch_entry(self):
        """'set_speaker_alias' must be in dispatch table mapping to self._speaker_manager.handle_set_speaker_alias."""
        self.assertIn("set_speaker_alias", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "set_speaker_alias"), "self._speaker_manager.handle_set_speaker_alias")

    def test_set_translation_glossary_item_dispatch_entry(self):
        """'set_translation_glossary_item' must be in dispatch table mapping to self._translation.handle_set_translation_glossary_item."""
        self.assertIn("set_translation_glossary_item", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "set_translation_glossary_item"), "self._translation.handle_set_translation_glossary_item")

    def test_start_call_assist_dispatch_entry(self):
        """'start_call_assist' must be in dispatch table mapping to self._call_assist.handle_start."""
        self.assertIn("start_call_assist", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "start_call_assist"), "self._call_assist.handle_start")

    def test_start_chain_dispatch_entry(self):
        """'start_chain' must be in dispatch table mapping to self._chains.handle_start_chain."""
        self.assertIn("start_chain", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "start_chain"), "self._chains.handle_start_chain")

    def test_stop_call_assist_dispatch_entry(self):
        """'stop_call_assist' must be in dispatch table mapping to self._call_assist.handle_stop."""
        self.assertIn("stop_call_assist", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "stop_call_assist"), "self._call_assist.handle_stop")

    def test_suggest_medical_glossary_terms_dispatch_entry(self):
        """'suggest_medical_glossary_terms' must be in dispatch table mapping to self._glossary_auto_learn.handle_suggest_medical_glossary_terms."""
        self.assertIn("suggest_medical_glossary_terms", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "suggest_medical_glossary_terms"), "self._glossary_auto_learn.handle_suggest_medical_glossary_terms")

    def test_summarize_item_dispatch_entry(self):
        """'summarize_item' must be in dispatch table mapping to self._text_processing_svc.handle_summarize_item."""
        self.assertIn("summarize_item", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "summarize_item"), "self._text_processing_svc.handle_summarize_item")

    def test_summarize_text_dispatch_entry(self):
        """'summarize_text' must be in dispatch table mapping to self._text_processing_svc.handle_summarize_text."""
        self.assertIn("summarize_text", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "summarize_text"), "self._text_processing_svc.handle_summarize_text")

    def test_synthesize_speech_dispatch_entry(self):
        """'synthesize_speech' must be in dispatch table mapping to self._tts.handle_synthesize_speech."""
        self.assertIn("synthesize_speech", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "synthesize_speech"), "self._tts.handle_synthesize_speech")

    def test_test_microphone_dispatch_entry(self):
        """'test_microphone' must be in dispatch table mapping to self._handle_test_microphone."""
        self.assertIn("test_microphone", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "test_microphone"), "self._handle_test_microphone")

    def test_toggle_favorite_dispatch_entry(self):
        """'toggle_favorite' must be in dispatch table mapping to self._history.handle_toggle_favorite."""
        self.assertIn("toggle_favorite", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "toggle_favorite"), "self._history.handle_toggle_favorite")

    def test_transcribe_paths_dispatch_entry(self):
        """'transcribe_paths' must be in dispatch table mapping to self._handle_transcribe_paths."""
        self.assertIn("transcribe_paths", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "transcribe_paths"), "self._handle_transcribe_paths")

    def test_transcribe_paths_async_dispatch_entry(self):
        """'transcribe_paths_async' must be in dispatch table mapping to self._handle_transcribe_paths_async."""
        self.assertIn("transcribe_paths_async", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "transcribe_paths_async"), "self._handle_transcribe_paths_async")

    def test_unarchive_items_dispatch_entry(self):
        """'unarchive_items' must be in dispatch table mapping to self._archive_manager.handle_unarchive_items."""
        self.assertIn("unarchive_items", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "unarchive_items"), "self._archive_manager.handle_unarchive_items")

    def test_unlink_recording_from_chain_dispatch_entry(self):
        """'unlink_recording_from_chain' must be in dispatch table mapping to self._chains.handle_unlink_recording_from_chain."""
        self.assertIn("unlink_recording_from_chain", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "unlink_recording_from_chain"), "self._chains.handle_unlink_recording_from_chain")

    def test_unload_plugin_dispatch_entry(self):
        """'unload_plugin' must be in dispatch table mapping to self._plugin_manager.handle_unload_plugin."""
        self.assertIn("unload_plugin", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "unload_plugin"), "self._plugin_manager.handle_unload_plugin")

    def test_unregister_webhook_dispatch_entry(self):
        """'unregister_webhook' must be in dispatch table mapping to self._webhook_manager.handle_unregister_webhook."""
        self.assertIn("unregister_webhook", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "unregister_webhook"), "self._webhook_manager.handle_unregister_webhook")

    def test_wake_word_list_models_dispatch_entry(self):
        """'wake_word_list_models' must be in dispatch table mapping to self._oww_adapter.handle_wake_word_list_models."""
        self.assertIn("wake_word_list_models", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "wake_word_list_models"), "self._oww_adapter.handle_wake_word_list_models")

    def test_wake_word_start_dispatch_entry(self):
        """'wake_word_start' must be in dispatch table mapping to self._oww_adapter.handle_wake_word_start."""
        self.assertIn("wake_word_start", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "wake_word_start"), "self._oww_adapter.handle_wake_word_start")

    def test_wake_word_status_dispatch_entry(self):
        """'wake_word_status' must be in dispatch table mapping to self._oww_adapter.handle_wake_word_status."""
        self.assertIn("wake_word_status", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "wake_word_status"), "self._oww_adapter.handle_wake_word_status")

    def test_wake_word_stop_dispatch_entry(self):
        """'wake_word_stop' must be in dispatch table mapping to self._oww_adapter.handle_wake_word_stop."""
        self.assertIn("wake_word_stop", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "wake_word_stop"), "self._oww_adapter.handle_wake_word_stop")

    def test_warmup_rewriter_dispatch_entry(self):
        """'warmup_rewriter' must be in dispatch table mapping to self._handle_warmup_rewriter."""
        self.assertIn("warmup_rewriter", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "warmup_rewriter"), "self._handle_warmup_rewriter")

    def test_warmup_stt_dispatch_entry(self):
        """'warmup_stt' must be in dispatch table mapping to self._stt_mgmt_svc.handle_warmup_stt."""
        self.assertIn("warmup_stt", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "warmup_stt"), "self._stt_mgmt_svc.handle_warmup_stt")

    def test_word_frequency_analysis_dispatch_entry(self):
        """'word_frequency_analysis' must be in dispatch table mapping to self._history.handle_word_frequency_analysis."""
        self.assertIn("word_frequency_analysis", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "word_frequency_analysis"), "self._history.handle_word_frequency_analysis")


if __name__ == "__main__":
    unittest.main(verbosity=2)
