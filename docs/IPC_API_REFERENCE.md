# Krab Ear IPC API Reference

Unix socket JSON-RPC protocol. Default socket paths:
- **Production (launchd):** `~/Library/Application Support/KrabEar/krabear.sock`
- **Dev standalone:** `~/.krab_ear_data/backend.sock`

**Request format:** `{"id": "req-1", "method": "...", "params": {...}}`  
**Success response:** `{"id": "req-1", "ok": true, "result": {...}}`  
**Error response:** `{"id": "req-1", "ok": false, "error": {"code": "...", "message": "..."}}`

---

## Recording

| Method | Description |
|---|---|
| `ping` | Liveness check, returns uptime and recording state |
| `start_recording` | Begin microphone capture |
| `stop_recording` | Stop capture, run STT, optionally translate, save to history |
| `get_recording_state` | Get current recording state and real-time preview text |
| `set_paste_status` | Update paste result for a history item |

### `ping`
No params.  
Returns: `{status, service, version, uptime_sec, is_recording, history_count}`

### `start_recording`
No params.  
Returns: `{status: "recording" | "already_recording", is_recording, duration_sec, preview_text}`

### `stop_recording`
Params (all optional, fall back to current settings):

| Param | Type | Default | Description |
|---|---|---|---|
| `quality_profile` | string | `"balanced"` | `"fast"`, `"balanced"`, `"max"` |
| `cleanup_profile` | string | `"soft"` | `"soft"`, `"strict"` |
| `lang_hint` | string | null | ISO 639-1 language hint |
| `translation_mode` | string | from settings | `"off"`, `"ru_to_es"`, `"es_to_ru"`, `"en_to_ru"`, `"auto"`, `"bilingual_ru_es"` |
| `translate_and_paste` | bool | from settings | Paste translated text instead of original |
| `stop_tail_trim_ms` | int | `180` | Trim N ms from end of audio (0–1200) |

Returns: `{status, text, original_text, translated_text, translation_status, translation_mode, source_lang, target_lang, history_id, ts, duration_sec, silence_detected, background_guard_rejected, ...}`  
Status values: `"ok"`, `"empty_audio"`, `"empty_text"`, `"already_stopped"`

### `get_recording_state`
No params.  
Returns: `{is_recording, duration_sec, preview_text}`

### `set_paste_status`
Params: `id` (str, required), `paste_status` (str: `"ok"` | `"failed"`)  
Returns: `{updated, id, paste_status}`

---

## History

| Method | Description |
|---|---|
| `get_history_page` | Paginated history with filters |
| `search_history` | Full-text search with filters |
| `search_by_speaker` | Filter by diarization speaker ID |
| `get_history_item` | Full details for single item by ID |
| `add_history_item` | Manually add item |
| `delete_history_item` | Soft-delete by ID (tombstone) |
| `compact_history` | Compact NDJSON, removing tombstones |
| `import_history_ndjson` | Import from external NDJSON file |
| `get_history_stats` | Journal file sizes and counts |
| `get_history_overview` | Dashboard overview snapshot |
| `get_history_statistics` | Aggregated stats (duration, words, dates) |
| `word_frequency_analysis` | Top N words by frequency |
| `cleanup_old_history` | Delete items older than N days |
| `get_clipboard_history` | Last 20 pasted items |
| `repaste_item` | Re-trigger paste for clipboard item |
| `get_storage_info` | Data file sizes |
| `get_transcripts_path` | Path to transcripts folder |
| `backup_history` | Create timestamped backup |
| `restore_history` | Restore from backup |
| `list_backups` | List available backups |
| `filter_by_confidence` | Filter history by STT confidence |
| `auto_summarize_batch` | LLM batch summarize recent items |

### `get_history_page`
Params (all optional):

| Param | Type | Default | Description |
|---|---|---|---|
| `cursor` | string | null | Pagination cursor from previous response |
| `limit` | int | `50` | Page size |
| `paste_status` | string | null | Filter: `"ok"` / `"failed"` |
| `translation_mode` | string | null | Filter by mode |
| `translation_status` | string | null | Filter by status |
| `from_ts` | string | null | ISO 8601 start datetime |
| `to_ts` | string | null | ISO 8601 end datetime |

Returns: `{items: [...HistoryItem], next_cursor}`

### `search_history`
Same params as `get_history_page` plus `query` (string, required).  
Returns: `{items, next_cursor}`

### `delete_history_item`
Params: `id` (str, required)  
Returns: `{deleted: true}`

### `add_history_item`
Params: `text` (str, required), optional `paste_status`, `source_text`, `translated_text`, `translation_mode`, `source_lang`, `target_lang`, `translation_status`, `translation_engine`  
Returns: full HistoryItem dict

---

## Tags

| Method | Description |
|---|---|
| `add_tag` | Add tag to item |
| `remove_tag` | Remove tag from item |
| `get_tags` | Get all tags for item |
| `search_by_tag` | Filter items by tag |
| `list_all_tags` | All tags with usage counts |

All tag methods require `id` (str). `add_tag` / `remove_tag` / `search_by_tag` also require `tag` (str).  
`search_by_tag` accepts optional `limit` (int, default 100, max 500).  
Returns: `{id, tags: [...]}` for item-level methods; `{tags: [{tag, count}, ...]}` for `list_all_tags`.

---

## Export

| Method | Description |
|---|---|
| `export_history` | Markdown with metadata and diarization |
| `export_history_srt` | SubRip subtitle format |
| `export_history_csv` | CSV table |
| `export_history_markdown` | Plain Markdown |
| `export_obsidian` | Obsidian-compatible `.md` |

All export methods accept optional `limit` (int, default 500, max 5000) and `save_to_file` (bool).  
Returns: `{content: str, total_items: int, path: str|null}`

---

## Translation

| Method | Description |
|---|---|
| `translate_text` | Translate arbitrary text |
| `set_translation_glossary_item` | Add/update glossary pair |
| `remove_translation_glossary_item` | Remove glossary entry |
| `get_glossary_suggestions` | Auto-suggest glossary pairs from history |
| `get_vocabulary_suggestions` | Propose STT vocabulary entries from history |

### `translate_text`
Params: `text` (str), `translation_mode` (str), optional `translation_style` (`"neutral"`, `"chat"`, `"formal"`), `network_mode`  
Returns: `{text, status, source_lang, target_lang, translation_mode, translation_style, engine}`

### `set_translation_glossary_item`
Params: `source` (str), `target` (str)  
Returns: `{glossary: {...}}`

### `remove_translation_glossary_item`
Params: `source` (str)  
Returns: `{glossary: {...}}`

---

## Settings

| Method | Description |
|---|---|
| `get_settings` | Read current settings (TTL-cached 5s) |
| `set_settings` | Merge-update settings |
| `apply_profile_preset` | Apply named preset |
| `list_profile_presets` | List available presets |
| `get_notification_preferences` | Read notification settings |
| `set_notification_preferences` | Update notification settings |

### `get_settings` / `set_settings`
`get_settings`: no params. Returns the full settings dict.  
`set_settings`: pass any subset of settings keys to merge. Key settings:

| Key | Type | Values |
|---|---|---|
| `quality_profile` | string | `"balanced"`, `"max"` |
| `cleanup_profile` | string | `"soft"`, `"strict"` |
| `translation_mode` | string | `"off"`, `"ru_to_es"`, `"es_to_ru"`, `"en_to_ru"`, `"auto"`, `"auto_to_ru"`, `"bilingual_ru_es"` |
| `translation_style` | string | `"neutral"`, `"chat"`, `"formal"` |
| `auto_paste` | bool | |
| `translate_and_paste` | bool | |
| `realtime_preview_enabled` | bool | |
| `network_mode` | string | `"offline_default"`, `"offline_strict"`, `"online_opt_in"` |
| `diarization_enabled` | bool | |
| `llm_rewrite_enabled` | bool | |

### `apply_profile_preset`
Params: `preset` (str: `"default"`, `"meeting"`, `"translation"`, `"call_recording"`)  
Returns: updated settings dict

---

## Diagnostics

| Method | Description |
|---|---|
| `get_diagnostics` | System, STT, LLM, history, settings cache info |
| `health_check` | Aggregated subsystem health |
| `analyze_audio_quality` | Pre-flight audio file quality metrics |
| `analyze_silence` | Silence/speech ratio in audio file |
| `detect_language` | Heuristic language detection |
| `get_last_llm_diff` | Last word-level LLM rewriter diff |
| `summarize_text` | Local lightweight text summarizer |
| `summarize_item` | LLM summary for history item by ID |

### `get_diagnostics`
No params.  
Returns: `{system: {python_version, platform, uptime_sec}, stt: {model_balanced, quality_profile, current_model, diarization_enabled, diarization_device}, llm: {...}, history: {total_items, data_dir}, settings_cache: {ttl_sec, cached}}`

### `analyze_audio_quality`
Params: `file_path` (str, required)  
Returns: `{rms_level, peak_level, snr_estimate_db, clipping_ratio, silence_ratio, duration_sec, quality_score, warnings}`

### `analyze_silence`
Params: `file_path` (str, required), `threshold_db` (float, default `-40.0`)  
Returns: `{silence_regions, speech_ratio, total_silence_sec, duration_sec}`

### `detect_language`
Params: `text` (str) **or** `texts` (list of str) for batch mode  
Returns: `{language, confidence, script}` or `{results: [{language, confidence, script}, ...]}`

### `summarize_text`
Params: `text` (str, required), `mode` (`"summary_short"` | `"summary_detailed"`, default `"summary_short"`), `max_points` (int 1–12, default 3)  
Returns: `{mode, summary, bullets, source_chars}`

### `summarize_item`
Params: `id` (str, required)  
Returns: `{id, summary, text_length, source_chars}`

---

## Analytics

| Method | Description |
|---|---|
| `get_recording_stats` | Cumulative recording statistics |
| `get_metrics_dashboard` | Real-time session/LLM/call_assist snapshot |
| `get_usage_stats` | Daily usage: recordings, duration, words |
| `get_error_report` | Recent errors from ring buffer |
| `get_error_stats` | Error counts by component/type/window |

### `get_recording_stats`
No params.  
Returns: `{total_count, total_duration_sec, today_count, today_duration_sec, week_count, week_duration_sec, avg_duration_sec, most_used_lang, lang_distribution, llm_applied_count, llm_correction_rate, diarization_used_count, diarization_usage_rate}`

### `get_metrics_dashboard`
No params.  
Returns: `{session: {recording_active, preview_active, preview_text_length, preview_duration_sec}, llm: {enabled, model, status}, call_assist: {...}, config_snapshot: {quality, cleanup, translation_mode, diarization, network_mode}}`

---

## System / Audio

| Method | Description |
|---|---|
| `list_audio_inputs` | Enumerate available audio input devices |
| `get_audio_devices` | Audio device list for GUI picker |
| `test_microphone` | Record short clip, return RMS/peak levels |

### `list_audio_inputs` / `get_audio_devices`
No params. Returns list of available input devices.

### `test_microphone`
No params. Returns: `{rms, peak, duration_sec, status}`

---

## Call Assist

| Method | Description |
|---|---|
| `start_call_assist` | Start real-time call translation session |
| `stop_call_assist` | Stop session and save results |
| `get_call_assist_state` | Current session state |
| `call_assist_diagnostics` | Session diagnostics |
| `call_assist_summary` | Session summary |
| `call_assist_quick_phrase` | Translate and emit a quick phrase |
| `list_call_assist_quick_phrases` | List available quick phrases |
| `call_assist_cost_estimate` | Estimated API cost for session |
| `call_assist_timeline` | Full timeline entries |
| `call_assist_timeline_stats` | Timeline statistics |
| `call_assist_timeline_summary` | LLM summary of timeline |
| `call_assist_timeline_export` | Export timeline to file |
| `call_assist_timeline_clear` | Clear timeline entries |
| `call_assist_timeline_to_history` | Save timeline to main history |

### `start_call_assist`
Params (all optional): `language_pair` (str), `voice_gateway_url` (str), `quality_profile` (str)  
Returns: `{status, session_id}`

### `stop_call_assist`
No params.  
Returns: `{status, duration_sec, timeline_count}`

### `call_assist_quick_phrase`
Params: `phrase` (str, required), `target_lang` (str, optional)  
Returns: `{translated, original, target_lang, engine}`

---

## Favorites

| Method | Description |
|---|---|
| `toggle_favorite` | Toggle favorite flag on a history item |
| `get_favorites` | List all favorited items |
| `is_favorite` | Check if item is favorited |

All require `id` (str). `toggle_favorite` / `is_favorite` return `{id, is_favorite}`. `get_favorites` returns `{items: [...]}`.

---

## Annotations

| Method | Description |
|---|---|
| `set_annotation` | Save a user note to a history item |
| `get_annotation` | Retrieve the note for a history item |
| `search_annotations` | Full-text search across notes |

`set_annotation` params: `id` (str), `text` (str). Returns `{id, annotation}`.  
`get_annotation` params: `id` (str). Returns `{id, annotation}`.  
`search_annotations` params: `query` (str), optional `limit` (int). Returns `{items: [...]}`.

---

## Collections

| Method | Description |
|---|---|
| `create_collection` | Create a named collection |
| `delete_collection` | Delete a collection |
| `list_collections` | List all collections |
| `add_to_collection` | Add a history item to a collection |
| `remove_from_collection` | Remove a history item from a collection |
| `get_collection_items` | Get items in a collection |

`create_collection` params: `name` (str), optional `description` (str). Returns collection dict.  
`add_to_collection` / `remove_from_collection` params: `collection_id` (str), `item_id` (str).  
`get_collection_items` params: `collection_id` (str), optional `limit` (int). Returns `{items: [...]}`.

---

## Recording Chains

| Method | Description |
|---|---|
| `start_chain` | Begin a chain of related recordings |
| `add_to_chain` | Add a history item to the active chain |
| `end_chain` | Finalize the chain |
| `get_chain` | Retrieve chain with all items |
| `list_chains` | List all chains |
| `merge_chain_text` | Get merged text of all items in a chain |

`start_chain` returns `{chain_id}`. `add_to_chain` params: `chain_id` (str), `item_id` (str).  
`get_chain` params: `chain_id` (str). Returns `{chain_id, items: [...], created_at, ended_at}`.  
`merge_chain_text` params: `chain_id` (str). Returns `{text, item_count}`.

---

## Recording Scheduler

| Method | Description |
|---|---|
| `schedule_recording` | Schedule a recording for a future time |
| `cancel_scheduled_recording` | Cancel a scheduled recording |
| `list_scheduled_recordings` | List all scheduled recordings |

`schedule_recording` params: `start_at` (ISO 8601 datetime), optional `duration_sec` (int), `quality_profile` (str).  
Returns `{job_id, start_at, status}`.

---

## Transcription Queue

| Method | Description |
|---|---|
| `enqueue_transcription` | Add an audio file to the transcription queue |
| `cancel_transcription` | Cancel a queued transcription job |
| `get_queue_status` | Get status of a transcription job |
| `list_transcription_queue` | List all queued transcription jobs |

`enqueue_transcription` params: `path` (str, required), optional `priority` (int 0–9), `quality_profile` (str).  
Returns `{job_id, status}`.  
`get_queue_status` / `cancel_transcription` params: `job_id` (str).

---

## Audio Tools

| Method | Description |
|---|---|
| `convert_audio` | Convert an audio file to WAV (default 16kHz mono) |
| `get_audio_info` | Get audio file metadata |
| `get_waveform` | Generate waveform data for GUI visualization |
| `check_audio_duplicate` | Audio fingerprinting to detect duplicate recordings |
| `detect_voice_activity` | VAD: detect speech/silence regions in audio file |
| `profile_noise` | Profile background noise: type, level, SNR, recommendations |

### `convert_audio`
Params: `input_path` (str, required), optional `output_format` (str, default `"wav"`), `sample_rate` (int, default 16000), `output_path` (str)  
Returns: `{output_path, format, sample_rate}`

### `get_audio_info`
Params: `path` (str, required)  
Returns: `{duration, sample_rate, channels, format, size_mb}`

### `get_waveform`
Params: `file_path` (str, required), optional `num_samples` (int)  
Returns: `{samples: [...float], duration_sec, sample_rate}`

### `check_audio_duplicate`
Params: `audio1` (list[float], PCM), `audio2` (list[float], PCM), optional `sample_rate` (int, default 16000), `threshold` (float, default 0.95)  
Returns: `{fingerprint1, fingerprint2, similarity, is_duplicate}`

### `detect_voice_activity`
Params: `file_path` (str, required), optional `threshold_db` (float)  
Returns: `{speech_regions, silence_regions, speech_ratio, duration_sec}`

### `profile_noise`
Params: `file_path` (str, required)  
Returns: `{noise_type, noise_level_db, snr_db, recommendations}`

---

## Export (extended)

| Method | Description |
|---|---|
| `export_history_json` | Export history as JSON |
| `export_html_report` | Standalone HTML analytics report |
| `batch_export` | Batch export in multiple formats simultaneously |
| `export_settings` | Export settings to a JSON file |
| `import_settings` | Import settings from a JSON file |

`batch_export` params: `formats` (list[str]), optional `limit` (int), `save_to_file` (bool).  
Returns: `{results: [{format, content, path}, ...]}`.

`export_settings` / `import_settings`: no required params (`import_settings` accepts `file_path` str).

---

## Obsidian Sync

| Method | Description |
|---|---|
| `configure_obsidian_sync` | Configure Obsidian vault path for sync |
| `run_obsidian_sync` | Sync history items to Obsidian vault |
| `get_obsidian_sync_status` | Get current sync status |

### `configure_obsidian_sync`
Params: `vault_path` (str, required), optional `folder` (str), `forced` (bool)  
Returns: `{vault_path, folder, enabled}`

### `run_obsidian_sync`
Params: optional `forced` (bool, default false), `limit` (int)  
Returns: `{synced, skipped, errors}`

### `get_obsidian_sync_status`
No params. Returns: `{vault_path, last_sync_at, total_synced, enabled}`

---

## Sharing

| Method | Description |
|---|---|
| `prepare_share` | Prepare a shareable package from history items |
| `list_shared` | List saved share packages |
| `get_shared` | Retrieve a share package by ID |

`prepare_share` params: `item_ids` (list[str]), optional `format` (str).  
Returns: `{share_id, content, created_at}`.  
`get_shared` params: `share_id` (str).

---

## Transcript Versioning

| Method | Description |
|---|---|
| `save_transcript_version` | Save a new version of a transcript text |
| `get_transcript_versions` | Get all versions of a transcript |
| `revert_transcript_version` | Revert to a specific version |

All require `item_id` (str). `save_transcript_version` also requires `text` (str).  
`revert_transcript_version` requires `version_id` (str).

---

## Playback Tracker

| Method | Description |
|---|---|
| `record_playback` | Register a playback event for a history item |
| `get_playback_stats` | Stats for one item: play_count, total listened |
| `get_most_replayed` | Top N most-replayed recordings |

`record_playback` params: `item_id` (str), `duration_listened_sec` (float).  
`get_most_replayed` params: optional `limit` (int, default 10).

---

## Text Analysis

| Method | Description |
|---|---|
| `extract_terms` | Extract key terms from transcript text |
| `compare_texts` | Structural diff/similarity between two texts |
| `score_readability` | Readability scoring (Flesch, sentence/word stats) |
| `score_transcription` | Quality score 0–100 with grade A–F |
| `analyze_speech_pace` | Speech pace: WPM, CPM, pace category |
| `get_keyword_cloud` | Keyword cloud data from history |
| `get_context_memory` | STT context memory: recent words and topics |
| `get_topic_timeline` | Topic-shift timeline from history |
| `anonymize_text` | Redact PII from transcript text |
| `detect_emotion` | Heuristic emotion detection in transcript |
| `compare_recordings` | Side-by-side comparison of multiple recordings |
| `post_process_text` | Run text through configurable post-processing pipeline |
| `list_post_process_steps` | List available post-processing steps |

### `extract_terms`
Params: `text` (str, required), optional `language` (str, default `"ru"`)  
Returns: `{terms: [{term, score, frequency, language, category}, ...]}`

### `compare_texts`
Params: `text1` + `text2` (str) **or** `item_id_1` + `item_id_2` (str IDs)  
Returns: `{similarity, text_1, text_2, common_phrases, unique_to_1, unique_to_2, word_count_diff, summary}`

### `score_readability`
Params: `text` (str, required)  
Returns: `{flesch_score, avg_sentence_length, avg_word_length, vocabulary_level, sentence_count, word_count, longest_sentence, shortest_sentence}`

### `score_transcription`
Params: `text` (str), `confidence` (float 0–1), `duration_sec` (float), optional `has_diarization` (bool), `has_llm_enhancement` (bool)  
Returns: `{overall_score, grade, factors, recommendations}`

### `analyze_speech_pace`
Params: `text` (str, required), `duration_sec` (float, required)  
Returns: `{words_per_minute, chars_per_minute, pace_category, estimated_reading_time_sec, word_count, char_count, duration_sec}`

### `get_keyword_cloud`
Params: optional `max_words` (int, default 100), `language` (str)  
Returns: `{words: [{word, count, weight, font_size}, ...]}`

### `get_context_memory`
Params: optional `max_words` (int, default 20), `last_n` (int, default 10), `clear` (bool)  
Returns: `{context_words, recent_topics, size, window_size}`

### `get_topic_timeline`
Params: optional `limit` (int, default 50), `days` (int)  
Returns: `{segments: [{topic, start_ts, items}, ...], total_shifts, current_topic}`

### `anonymize_text`
Params: `text` (str, required), optional `rules` (list[str]: `"phone"`, `"email"`, `"credit_card"`, etc.)  
Returns: `{anonymized_text, redaction_count, redactions: [{original, replacement, category, position}, ...]}`

### `detect_emotion`
Params: `text` (str, required), optional `language` (str, default `"ru"`)  
Returns: `{primary_emotion, confidence, indicators, exclamation_count, question_count, caps_ratio}`

### `compare_recordings`
Params: `item_ids` (list[str], required)  
Returns: `{items, similarity_matrix, common_words, unique_words_per_item, stats}`

### `post_process_text`
Params: `text` (str, required), optional `steps` (list[str]; default: strip_whitespace, fix_punctuation, normalize_entities)  
Returns: `{text, steps_applied, changes_count}`

### `list_post_process_steps`
No params. Returns: `{steps: [...]}`

---

## Analytics (extended)

| Method | Description |
|---|---|
| `generate_daily_digest` | Daily digest summary of transcription activity |
| `analyze_quality_trends` | Confidence/quality trends over N days |
| `get_recording_insights` | Heuristic insights about recordings |
| `get_sentiment_trends` | Sentiment trend analysis over N days |
| `compare_periods` | Compare usage stats between two time periods |
| `get_analytics_dashboard` | All analytics metrics in a single call |
| `find_duplicates` | Detect duplicate transcriptions by text similarity |

### `generate_daily_digest`
Params: optional `date` (ISO date string; default today)  
Returns: `{date, total_recordings, total_duration_min, total_words, languages_used, top_topics, highlights, markdown}`

### `analyze_quality_trends`
Params: optional `days` (int, default 30)  
Returns: `{daily_confidence, overall_trend, trend_slope, best_day, worst_day, confidence_distribution}`

### `get_recording_insights`
Params: optional `days` (int, default 7)  
Returns: `{insights: [{type, message, severity}, ...], count, days}`

### `get_sentiment_trends`
Params: optional `days` (int, default 30)  
Returns: `{daily_sentiment, overall_trend, mood_shift_count, dominant_emotion}`

### `compare_periods`
Params: `period1_start`, `period1_end`, `period2_start`, `period2_end` (ISO 8601 datetimes, all required)  
Returns: `{period1: {...}, period2: {...}, recordings_change_pct, duration_change_pct, confidence_change, new_languages, summary}`

### `get_analytics_dashboard`
No params. Returns comprehensive analytics snapshot with all metrics.

### `find_duplicates`
Params: optional `threshold` (float 0–1, default 0.9), `limit` (int)  
Returns: `{groups: [[item_id, ...], ...], total_duplicates}`

---

## Integrity & Migration

| Method | Description |
|---|---|
| `check_integrity` | Validate NDJSON data store integrity |
| `repair_integrity` | Auto-fix fixable integrity problems |
| `check_migration` | Check if data migration is needed |
| `run_migration` | Execute data migration between versions |

### `check_integrity`
No params.  
Returns: `{status, total_items, orphaned_tombstones, invalid_json_lines, checks: [{name, status, message, auto_fixable}, ...]}`

### `repair_integrity`
No params.  
Returns: `{fixed, skipped, details}`

### `check_migration` / `run_migration`
No required params.  
`check_migration` returns `{needs_migration, current_version, target_version}`.  
`run_migration` returns `{migrated, details}`.

---

## Abbreviations

| Method | Description |
|---|---|
| `expand_abbreviations` | Expand abbreviations in transcript text |
| `add_abbreviation` | Add a custom abbreviation |
| `remove_abbreviation` | Remove an abbreviation |
| `list_abbreviations` | List all abbreviations for a language |

`expand_abbreviations` params: `text` (str), optional `language` (str, default `"ru"`). Returns: `{expanded, changed}`.  
`add_abbreviation` params: `abbr` (str), `expansion` (str), optional `language` (str), `flags` (str). Returns: `{ok: true}`.  
`remove_abbreviation` params: `abbr` (str), optional `language` (str). Returns: `{removed: bool}`.  
`list_abbreviations` params: optional `language` (str). Returns: `{abbreviations, language, count}`.

---

## Text Formatting

| Method | Description |
|---|---|
| `format_for_paste` | Format text for a specific target application |
| `list_paste_formatters` | List available paste format targets |

`format_for_paste` params: `text` (str, required), `target` (str: `"telegram"`, `"notes"`, `"email"`, etc.)  
Returns: `{formatted, target, changes}`.

---

## Model Selection

| Method | Description |
|---|---|
| `select_model` | Smart STT model selection based on recording conditions |
| `auto_update_vocabulary` | Smart auto-update STT vocabulary from history |
| `get_smart_vocabulary_suggestions` | STT vocabulary suggestions from usage patterns |

### `select_model`
Params: `duration_sec` (float, required), optional `quality` (str, default `"balanced"`), `is_preview` (bool), `system_load` (float 0–1)  
Returns: `{model_name, reason, estimated_latency_ms, quality_tier}`

### `auto_update_vocabulary`
Params: optional `min_frequency` (int, default 3), `scan_limit` (int, default 200)  
Returns: `{new_words, removed_words, total, sources}`

### `get_smart_vocabulary_suggestions`
Params: optional `scan_limit` (int, default 100), `min_frequency` (int, default 2), `top_k` (int, default 30)  
Returns: `{suggestions, total}`

---

## Config Presets Library

| Method | Description |
|---|---|
| `list_config_presets` | List built-in and custom config presets |
| `apply_config_preset` | Get settings patch for a preset |
| `create_config_preset` | Create a custom config preset |

`list_config_presets` returns `{presets: [{name, description, is_custom}, ...]}`.  
`apply_config_preset` params: `name` (str). Returns: `{settings_patch: {...}}`.  
`create_config_preset` params: `name` (str), `description` (str), `settings_patch` (dict).

---

## Normalization Profiles

| Method | Description |
|---|---|
| `list_normalization_profiles` | List text normalization profiles |
| `apply_normalization_profile` | Apply a normalization profile to text |

`apply_normalization_profile` params: `text` (str, required), `profile` (str).  
Returns: `{text, profile, changed}`.

---

## Language Learning

| Method | Description |
|---|---|
| `extract_learning_vocabulary` | Extract vocabulary from bilingual transcripts |
| `generate_flashcards` | Generate flashcards for language learning |
| `get_learning_stats` | Language learning progress statistics |

`extract_learning_vocabulary` params: optional `limit` (int), `language_pair` (str).  
Returns: `{vocabulary: [{word, translation, examples}, ...]}`.  
`generate_flashcards` params: optional `limit` (int, default 20). Returns: `{cards: [{front, back}, ...]}`.  
`get_learning_stats`: no params. Returns: `{words_seen, cards_generated, top_words}`.

---

## Cost Estimation

| Method | Description |
|---|---|
| `estimate_recording_cost` | Estimate compute cost for processing a recording |
| `get_daily_cost_summary` | Today's cumulative compute cost summary |

### `estimate_recording_cost`
Params: `duration_sec` (float, required), optional `quality` (str: `"balanced"`, `"max"`, `"remote"`), `features` (obj: `{diarization, llm, translation}` bools)  
Returns: `{compute_time_sec, memory_mb, disk_mb, features_cost, total_relative_cost}`

### `get_daily_cost_summary`
No params. Returns today's cost rollup dict.

---

## Event Replay

| Method | Description |
|---|---|
| `get_event_log` | Event log for debugging (filter by type/time) |
| `get_event_stats` | Event counters and rate per minute |
| `replay_events` | Replay events in a time range |

`get_event_log` params: optional `event_type` (str), `since` (ISO 8601), `limit` (int, default 100).  
Returns: `{events: [...], count}`.  
`replay_events` params: `since` (str), optional `until` (str), `event_types` (list[str]).

---

## Auto Backup & Export Schedule

| Method | Description |
|---|---|
| `get_auto_backup_status` | Status of automatic backup |
| `configure_auto_export` | Configure auto-export schedule |
| `get_export_schedule_status` | Status of the auto-export schedule |
| `list_auto_exports` | List auto-exported files |

### `configure_auto_export`
Params: `format` (str: `srt`, `csv`, `markdown`, `json`, `obsidian`, `html`), optional `interval_hours` (int, default 24), `output_dir` (str), `enabled` (bool, default true)  
Returns updated schedule status dict.

---

## IPC Utilities

| Method | Description |
|---|---|
| `batch` | Execute multiple IPC methods in one call (max 50) |
| `get_throttle_stats` | IPC throttle stats: calls, rejections per method |
| `get_startup_diagnostics` | Startup diagnostics: all check results |
| `get_shutdown_status` | Status of the last graceful shutdown |
| `get_system_info` | System resource monitoring: CPU, RAM, disk, GPU |
| `search_with_highlights` | Search with match highlights in results |
| `fuzzy_search` | Fuzzy/approximate search over history |
| `transcribe_paths` | Transcribe audio files by path list |
| `preview_transcribe_paths` | Preview transcription of audio paths |
| `merge_recordings` | Merge multiple history items into one |
| `preview_merge` | Preview merge result without saving |
| `enrich_recording` | Auto-enrich recording metadata (word_count, emotion, pace, quality, topics) |

### `batch`
Params: `requests` (list of `{method, params?}`, max 50)  
Returns: `{results: [{method, ok, result|error}, ...], total, succeeded, failed}`

### `fuzzy_search`
Params: `query` (str, required), optional `limit` (int), `threshold` (float)  
Returns: `{items: [...], total}`

### `search_with_highlights`
Params: `query` (str, required), optional `limit` (int)  
Returns: `{items: [{...item, highlights: [str]}, ...]}`

### `transcribe_paths`
Params: `paths` (list[str], required), optional `quality_profile`, `cleanup_profile`, `lang_hint`, `translation_mode`, `translate_and_paste`  
Returns: `{items: [...HistoryItem], processed, errors}`

### `preview_transcribe_paths`
Params: `paths` (list[str], required)  
Returns preview text without saving to history.

### `merge_recordings`
Params: `item_ids` (list[str], required), optional `separator` (str)  
Returns merged HistoryItem dict.

### `preview_merge`
Params: `item_ids` (list[str], required)  
Returns `{text, item_count, total_duration_sec}` without saving.

### `enrich_recording`
Params: `item_id` (str, required)  
Returns enriched metadata dict: `{word_count, emotion, pace, quality_score, topics}`.

### `get_system_info`
No params. Returns: `{cpu_percent, memory_percent, disk_percent, gpu_info}`

### `get_startup_diagnostics`
No params. Returns: `{status, checks: [...], startup_time_ms, errors, warnings}`

### `get_shutdown_status`
No params. Returns: `{clean, last_shutdown_time}`
