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
| `get_session_history` | Past recording sessions with metadata |
| `get_session_stats` | Aggregated session statistics |
| `get_usage_stats` | Daily usage: recordings, duration, words |
| `get_error_report` | Recent errors from ring buffer |
| `get_error_stats` | Error counts by component/type/window |

### `get_recording_stats`
No params.  
Returns: `{total_count, total_duration_sec, today_count, today_duration_sec, week_count, week_duration_sec, avg_duration_sec, most_used_lang, lang_distribution, llm_applied_count, llm_correction_rate, diarization_used_count, diarization_usage_rate}`

### `get_session_history`
Params: `limit` (int, default 50)  
Returns: `{sessions: [...], count}`

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
