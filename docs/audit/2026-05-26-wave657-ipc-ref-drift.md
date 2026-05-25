# Wave 657 — IPC_API_REFERENCE Drift Audit

**Date:** 2026-05-26  
**Branch:** wave657/ipc-ref-drift

## Counts

| Source | Count |
|--------|-------|
| Dispatch handlers in `service.py` | **86** |
| H2 sections in `IPC_API_REFERENCE.md` | **35** (categories, not methods) |
| H3 method entries in `IPC_API_REFERENCE.md` | **79** (76 headers → 79 unique names after multi-method headers split) |

## Summary

- **51 handlers dispatched but NOT documented** (added after PR #243 cut the reference)
- **44 methods documented but NOT in dispatch** (removed from service.py or renamed since PR #243)

Net drift: reference is **stale by ~58%** of the live handler surface.

## Handlers in dispatch NOT documented (51)

```
add_stt_hotword, batch_extract_action_items, cancel_transcribe_job,
check_duplicate, clear_privacy_audit_log, clear_recent_errors,
create_apple_note, create_apple_reminder, create_calendar_event,
export_glossary_csv, extract_action_items, generate_auto_title,
generate_mini_stats_report, generate_stats_report, get_activity_calendar,
get_dedup_stats, get_last_llm_diff, get_learning_stats, get_memory_stats,
get_pending_action_items, get_privacy_audit_log, get_stt_routing_decision,
get_throttle_stats, get_timeline_view, get_transcribe_progress,
get_usage_stats, handle_error_action, handshake, health_check,
import_glossary_csv, list_llm_models, list_normalization_profiles,
list_recent_errors, list_stt_hotwords, list_telegram_chats,
probe_llm_http, remove_stt_hotword, replace_word_in_last_transcript,
report_hotkey_conflict, report_paste_failure, report_reconnect,
run_deduplication, semantic_search, semantic_search_reindex,
semantic_search_status, send_diagnostics_to_sentry, send_imessage,
send_to_telegram, transcribe_paths_async, warmup_rewriter, warmup_stt
```

## Methods documented but NOT in dispatch (44)

These are phantom entries — likely removed/renamed handlers or methods that migrated to extracted services and whose dispatch was removed without updating the doc.

```
add_history_item, analyze_audio_quality, analyze_quality_trends,
analyze_silence, analyze_speech_pace, anonymize_text, apply_profile_preset,
auto_update_vocabulary, call_assist_quick_phrase, check_audio_duplicate,
check_migration, compare_texts, configure_obsidian_sync, convert_audio,
delete_history_item, detect_emotion, detect_language, detect_voice_activity,
enrich_recording, find_duplicates, fuzzy_search, get_audio_info,
get_history_page, get_obsidian_sync_status, get_settings, get_waveform,
list_post_process_steps, merge_recordings, post_process_text, preview_merge,
profile_noise, remove_translation_glossary_item, run_migration,
run_obsidian_sync, score_readability, search_history, search_with_highlights,
set_settings, set_translation_glossary_item, start_call_assist,
stop_call_assist, summarize_item, summarize_text, translate_text
```

## Recommended next steps

1. **Verify phantom entries**: grep `_handle_<method>` in `service.py` — some may be in extracted services (`CallAssistService`, `HistoryService`, etc.) and still callable via delegation but not in the main dispatch table. If so, mark as "delegated" in the reference, not remove.
2. **Add 51 missing entries**: high priority are Phase B IPC additions (`handle_error_action`, `list_recent_errors`, `clear_recent_errors`, `handshake`, `report_*`, `probe_llm_http`, `warmup_*`) and semantic search group.
3. **Prune 44 phantom entries** after confirming they are truly gone from all dispatch paths.
4. **Add CI check**: `scripts/verify_ipc_ref.py` — diff dispatch table vs H3 headers, fail if delta > 0.
