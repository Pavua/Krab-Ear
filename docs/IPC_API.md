# Krab Ear IPC API — Comprehensive Handler Reference

All 241+ JSON-RPC methods exposed by the Python backend over a Unix socket.

---

## 1. Protocol Overview

Krab Ear backend communicates with the Swift agent (and any other client) via a Unix domain socket using a JSON-RPC-like protocol.

**Socket paths:**

| Mode | Path |
|------|------|
| Production (launchd Variant B) | `~/Library/Application Support/KrabEar/krabear.sock` |
| Dev standalone (`--data-dir`) | `~/.krab_ear_data/backend.sock` |

**Request envelope:**

```json
{
  "id": "req-001",
  "method": "get_history_stats",
  "params": { "days": 7 }
}
```

**Success response:**

```json
{
  "id": "req-001",
  "ok": true,
  "result": { ... }
}
```

**Error response:**

```json
{
  "id": "req-001",
  "ok": false,
  "error": { "code": "unknown_method", "message": "..." }
}
```

**Optional IPC signing** (when `IPC_SIGNING_SECRET` env var is set):

```json
{
  "id": "req-001",
  "method": "...",
  "params": {},
  "signature": "<hmac-sha256-hex>",
  "timestamp": 1714000000.0,
  "nonce": "abc123"
}
```

---

## 2. Common Error Codes

| Code | Description |
|------|-------------|
| `unknown_method` | Method name not found in dispatch table |
| `invalid_params` | `params` field is not a JSON object, or required param missing/wrong type |
| `rate_limit_exceeded` | Token-bucket rate limit hit; response includes `wait_sec` |
| `unauthorized` | IPC signing enabled and signature missing or invalid |
| `internal_error` | Unhandled exception inside handler; message contains Python traceback summary |

---

## 3. Rate Limit Classes

Rate limits are enforced via a token-bucket algorithm (`backend/ipc_throttle.py`).

| Class | Limit | Methods |
|-------|-------|---------|
| **heavy** | 5 / min | CPU/GPU-intensive ops: transcription, export, summarize, integrity check, audio analysis |
| **medium** | 30 / min | Search, statistics, diagnostics, translate, event log |
| **light** | 120 / min | Everything else not listed above |
| **excluded** | unlimited | `ping`, `start_recording`, `stop_recording`, `get_recording_state`, `set_paste_status`, `get_settings`, `set_settings`, `apply_profile_preset`, `list_profile_presets`, `translate_selection`, `live_subs_ingest`, `live_subs_stop`, `call_estimate_cost`, `call_check_auto_end` |

When rate limit is exceeded the error response includes:

```json
{
  "ok": false,
  "error": { "code": "rate_limit_exceeded", "message": "... Повторите через 12.3s" }
}
```

---

## 4. Handler Reference

### 4.1 Recording Lifecycle

---

#### `ping`
**Rate:** excluded | **Caller:** Swift BackendSupervisor

Liveness check. Returns service metadata and current recording state.

**Params:** none

**Returns:**

```json
{
  "status": "ok",
  "service": "krabear-backend",
  "version": "2.x.x",
  "uptime_sec": 123.4,
  "is_recording": false,
  "history_count": 42
}
```

---

#### `start_recording`
**Rate:** excluded | **Caller:** Swift main

Begin microphone capture. Idempotent — calling while already recording returns `already_recording` without error. Also starts the real-time preview worker if `realtime_preview_enabled` is true.

**Params:** none

**Returns:**

```json
{ "status": "recording" }
```

or (if already recording):

```json
{
  "status": "already_recording",
  "is_recording": true,
  "duration_sec": 5.2,
  "preview_text": "partial transcript..."
}
```

---

#### `stop_recording`
**Rate:** excluded | **Caller:** Swift main

Stop capture, run STT pipeline (normalise → transcribe → cleanup → diarize → translate → LLM rewrite), save to history, and optionally paste.

**Params (all optional, fall back to saved settings):**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `quality_profile` | string | `"balanced"` | `"fast"`, `"balanced"`, `"max"` |
| `cleanup_profile` | string | `"soft"` | `"soft"`, `"strict"` |
| `lang_hint` | string | null | ISO 639-1 hint (e.g. `"ru"`, `"es"`) |
| `translation_mode` | string | from settings | `"off"`, `"ru_to_es"`, `"es_to_ru"`, `"en_to_ru"`, `"auto"`, `"bilingual_ru_es"` |
| `translation_style` | string | `"neutral"` | `"neutral"`, `"formal"`, `"casual"` |
| `translate_and_paste` | bool | from settings | Paste translated text instead of original |
| `stop_tail_trim_ms` | int | 180 | Milliseconds trimmed from end of recording (0–1200) |

**Returns:**

```json
{
  "status": "ok",
  "text": "transcribed text",
  "original_text": "pre-translation text",
  "translated_text": "...",
  "translation_status": "ok",
  "translation_mode": "ru_to_es",
  "source_lang": "ru",
  "target_lang": "es",
  "history_id": "uuid",
  "ts": "2026-04-25T10:00:00",
  "duration_sec": 8.3,
  "confidence": 0.91,
  "silence_detected": false,
  "background_guard_rejected": false,
  "diarization": [...],
  "quality_profile": "balanced",
  "cleanup_profile": "soft",
  "llm_rewrite_applied": true
}
```

Status values: `"ok"`, `"empty_audio"`, `"empty_text"`, `"already_stopped"`.

---

#### `get_recording_state`
**Rate:** excluded | **Caller:** Swift main, HistoryPanel

Returns current recording status and live preview text.

**Params:** none

**Returns:**

```json
{
  "is_recording": true,
  "duration_sec": 4.1,
  "preview_text": "real-time partial text"
}
```

---

#### `set_paste_status`
**Rate:** excluded | **Caller:** Swift main

Update paste result on a history item after Swift accessibility paste completes.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | History item UUID |
| `paste_ok` | bool | yes | Whether paste succeeded |

**Returns:** `{ "ok": true }`

---

#### `list_audio_inputs`
**Rate:** light | **Caller:** Swift HistoryPanel

List available microphone inputs via `sounddevice`.

**Params:** none

**Returns:**

```json
{
  "devices": [
    { "id": 0, "name": "Built-in Microphone", "channels": 2, "default": true }
  ]
}
```

---

#### `get_audio_devices`
**Rate:** light | **Caller:** Swift HistoryPanel

Same as `list_audio_inputs` (alias for GUI audio device picker).

**Params:** none

**Returns:** same as `list_audio_inputs`

---

#### `test_microphone`
**Rate:** light | **Caller:** Swift HistoryPanel

Record a short clip and return RMS/peak levels as a microphone quality check.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `duration_sec` | float | 2.0 | Clip duration |
| `device_id` | int | null | Specific input device ID |

**Returns:**

```json
{
  "rms": 0.012,
  "peak": 0.045,
  "clipping": false,
  "ok": true
}
```

---

#### `get_session_history`
**Rate:** medium | **Caller:** internal

Return list of past recording sessions with metadata.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max sessions to return |

**Returns:** `{ "sessions": [ { "session_id", "started_at", "stopped_at", "duration_sec", "transcript_count" } ] }`

---

#### `get_session_stats`
**Rate:** medium | **Caller:** internal

Aggregate stats across all sessions (total duration, count, avg quality).

**Params:** none

**Returns:** `{ "total_sessions", "total_duration_sec", "avg_duration_sec", "avg_confidence" }`

---

#### `get_recording_stats`
**Rate:** medium | **Caller:** internal

Recording metadata statistics (aliases `get_recording_insights`).

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** aggregated recording stats dict.

---

#### `get_shutdown_status`
**Rate:** light | **Caller:** internal

Returns status of the last graceful shutdown.

**Params:** none

**Returns:** `{ "clean": true, "last_shutdown_time": "ISO8601" }`

---

### 4.2 Audio Import & Transcription

---

#### `transcribe_paths`
**Rate:** heavy | **Caller:** Swift HistoryPanel

Synchronously transcribe one or more audio files. Blocks until complete.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `paths` | string[] | yes | Absolute paths to audio files |
| `quality_profile` | string | no | `"fast"`, `"balanced"`, `"max"` |
| `translation_mode` | string | no | Translation mode |
| `lang_hint` | string | no | Language hint |

**Returns:** `{ "results": [ { "path", "history_id", "text", "status", "error" } ] }`

---

#### `transcribe_paths_async`
**Rate:** light | **Caller:** Swift HistoryPanel

Start a background transcription job. Returns immediately with a `job_id`.

**Params:** same as `transcribe_paths`

**Returns:** `{ "job_id": "uuid" }`

---

#### `get_transcribe_progress`
**Rate:** light | **Caller:** Swift HistoryPanel

Poll progress of an async transcription job.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | Job UUID from `transcribe_paths_async` |

**Returns:** `{ "job_id", "status": "running|done|cancelled|error", "progress": 0.75, "results": [...] }`

---

#### `cancel_transcribe_job`
**Rate:** light | **Caller:** Swift HistoryPanel

Request cancellation of an async transcription job.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | Job UUID |

**Returns:** `{ "cancelled": true }`

---

#### `preview_transcribe_paths`
**Rate:** heavy | **Caller:** Swift HistoryPanel

Estimate transcription time/cost without actually transcribing.

**Params:** same as `transcribe_paths`

**Returns:** `{ "files": [ { "path", "duration_sec", "estimated_time_sec", "size_mb" } ], "total_duration_sec" }`

---

#### `convert_audio`
**Rate:** heavy | **Caller:** internal

Convert an audio file to WAV via ffmpeg.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Source audio path |
| `output_path` | string | no | Destination path (default: temp) |

**Returns:** `{ "output_path", "duration_sec", "sample_rate", "channels" }`

---

#### `get_audio_info`
**Rate:** light | **Caller:** internal

Get audio file metadata (duration, codec, sample rate, channels).

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |

**Returns:** `{ "path", "duration_sec", "sample_rate", "channels", "codec", "bit_rate", "size_mb" }`

---

#### `analyze_audio_quality`
**Rate:** heavy | **Caller:** internal

Pre-flight analysis of an audio file: RMS, peak, SNR, clipping ratio, silence ratio.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |

**Returns:** `{ "rms", "peak", "snr_db", "clipping_ratio", "silence_ratio", "quality_score", "recommendations": [] }`

---

#### `analyze_silence`
**Rate:** heavy | **Caller:** internal

Detect silence regions and speech ratio in an audio file.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |
| `threshold_db` | float | -40 | Silence threshold in dBFS |

**Returns:** `{ "silence_regions": [ { "start_sec", "end_sec" } ], "speech_ratio", "total_silence_sec" }`

---

#### `detect_voice_activity`
**Rate:** light | **Caller:** internal

VAD over an audio file: detect speech/silence segments.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |

**Returns:** `{ "segments": [ { "start_sec", "end_sec", "type": "speech|silence" } ] }`

---

#### `profile_noise`
**Rate:** light | **Caller:** internal

Characterise background noise type, level, and SNR.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |

**Returns:** `{ "noise_type", "noise_level_db", "snr_db", "recommendations": [] }`

---

#### `get_waveform`
**Rate:** heavy | **Caller:** internal

Downsample PCM for GUI waveform visualisation.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |
| `points` | int | 200 | Number of waveform data points |

**Returns:** `{ "waveform": [0.0, 0.12, ...], "duration_sec" }`

---

#### `check_audio_duplicate`
**Rate:** light | **Caller:** internal

Audio fingerprint-based duplicate detection against existing history.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |

**Returns:** `{ "is_duplicate": false, "match_id": null, "similarity": 0.0 }`

---

#### `analyze_word_timing`
**Rate:** light | **Caller:** internal

Analyse speech rhythm from per-word timestamps in Whisper output.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | History item with word-level timestamps |

**Returns:** `{ "avg_word_gap_ms", "longest_pause_ms", "speech_density" }`

---

#### `analyze_speech_pace`
**Rate:** light | **Caller:** internal

Speech pace analysis: words per minute, characters per minute, pace category.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Transcript text |
| `duration_sec` | float | yes | Audio duration |

**Returns:** `{ "wpm", "cpm", "pace_category": "slow|normal|fast|very_fast" }`

---

### 4.3 History Management

---

#### `get_history_page`
**Rate:** light | **Caller:** Swift HistoryPanel

Paginated history retrieval.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `page` | int | 0 | Page index (0-based) |
| `page_size` | int | 20 | Items per page |
| `include_archived` | bool | false | Include archived items |

**Returns:** `{ "items": [...], "total", "page", "page_size", "has_more" }`

---

#### `get_history_item`
**Rate:** light | **Caller:** internal

Full details for a single history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** Full history item object.

---

#### `get_history_stats`
**Rate:** medium | **Caller:** Swift HistoryPanel

Summary statistics for history (total items, duration, word count, language distribution).

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** `{ "total_items", "total_duration_sec", "total_words", "languages": { "ru": 80, "es": 20 } }`

---

#### `get_history_overview`
**Rate:** medium | **Caller:** Swift HistoryPanel

High-level history overview for the dashboard panel.

**Params:** same as `get_history_stats`

**Returns:** overview dict with recent activity, top languages, etc.

---

#### `get_history_statistics`
**Rate:** medium | **Caller:** internal

Aggregated statistics across all history (hourly/daily/weekly breakdown).

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** detailed stats dict with temporal breakdowns.

---

#### `search_history`
**Rate:** medium | **Caller:** Swift HistoryPanel

Full-text search over transcription history.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Search query |
| `limit` | int | no | Max results (default 20) |
| `days` | int | no | Restrict to last N days |

**Returns:** `{ "items": [...], "total" }`

---

#### `fuzzy_search`
**Rate:** medium | **Caller:** internal

Approximate string matching search (handles typos and phonetic similarity).

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Query string |
| `threshold` | float | no | Similarity threshold 0–1 (default 0.6) |
| `limit` | int | no | Max results |

**Returns:** `{ "items": [...] }`

---

#### `search_with_highlights`
**Rate:** light | **Caller:** internal

Search with match-highlighted snippets in results.

**Params:** same as `search_history`

**Returns:** `{ "items": [ { ...item, "highlights": ["...match snippet..."] } ] }`

---

#### `search_by_speaker`
**Rate:** medium | **Caller:** internal

Search history items by diarized speaker label.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `speaker` | string | yes | Speaker label (e.g. `"SPEAKER_00"`) |
| `limit` | int | no | Max results |

**Returns:** `{ "items": [...] }`

---

#### `delete_history_item`
**Rate:** light | **Caller:** Swift HistoryPanel

Soft-delete (tombstone) a history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "deleted": true }`

---

#### `add_history_item`
**Rate:** light | **Caller:** Swift HistoryPanel

Manually add a history item (e.g. imported transcript).

**Params:** History item fields (text, ts, duration_sec, etc.)

**Returns:** `{ "history_id": "uuid" }`

---

#### `compact_history`
**Rate:** light | **Caller:** Swift main, HistoryPanel

Compact the NDJSON history file (remove tombstoned records).

**Params:** none

**Returns:** `{ "items_before", "items_after", "size_before_bytes", "size_after_bytes" }`

---

#### `import_history_ndjson`
**Rate:** light | **Caller:** Swift HistoryPanel

Import history items from an NDJSON file.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Path to NDJSON file |

**Returns:** `{ "imported", "skipped", "errors": [] }`

---

#### `toggle_favorite`
**Rate:** light | **Caller:** internal

Toggle the favorite flag on a history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "is_favorite": true }`

---

#### `get_favorites`
**Rate:** light | **Caller:** internal

Retrieve all favorited history items.

**Params:** none

**Returns:** `{ "items": [...] }`

---

#### `is_favorite`
**Rate:** light | **Caller:** internal

Check if a single item is favorited.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "is_favorite": false }`

---

#### `repaste_item`
**Rate:** light | **Caller:** Swift HistoryPanel

Re-trigger accessibility paste for an existing history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "ok": true }`

---

#### `get_clipboard_history`
**Rate:** light | **Caller:** internal

Last 20 paste items stored in memory.

**Params:** none

**Returns:** `{ "items": [ { "history_id", "text", "pasted_at" } ] }`

---

#### `cleanup_old_history`
**Rate:** light | **Caller:** internal

Delete history entries older than N days.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `days` | int | yes | Age threshold |

**Returns:** `{ "deleted": 15 }`

---

#### `get_storage_info`
**Rate:** light | **Caller:** internal

Return file sizes for all data files (history, settings, transcripts).

**Params:** none

**Returns:** `{ "history_bytes", "settings_bytes", "transcripts_bytes", "total_bytes" }`

---

#### `get_transcripts_path`
**Rate:** light | **Caller:** internal

Return the path to the transcripts directory.

**Params:** none

**Returns:** `{ "path": "/Users/.../Library/Application Support/KrabEar/transcripts" }`

---

#### `backup_history`
**Rate:** light | **Caller:** internal

Create a timestamped backup of the history NDJSON file.

**Params:** none

**Returns:** `{ "backup_path", "size_bytes" }`

---

#### `restore_history`
**Rate:** light | **Caller:** internal

Restore history from a backup file.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `backup_path` | string | yes | Path to backup file |

**Returns:** `{ "restored_items" }`

---

#### `list_backups`
**Rate:** light | **Caller:** internal

List available history backup files.

**Params:** none

**Returns:** `{ "backups": [ { "path", "created_at", "size_bytes" } ] }`

---

#### `word_frequency_analysis`
**Rate:** medium | **Caller:** internal

Word frequency analysis across history.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |
| `top_n` | int | 50 | Number of top words |

**Returns:** `{ "words": [ { "word", "count", "freq" } ] }`

---

#### `find_duplicates`
**Rate:** medium | **Caller:** internal

Detect duplicate transcriptions by text similarity.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `threshold` | float | 0.85 | Similarity threshold |

**Returns:** `{ "groups": [ [ "id1", "id2" ] ], "total_duplicates" }`

---

#### `check_duplicate`
**Rate:** light | **Caller:** internal

Check a single text against history for duplicate detection.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Text to check |

**Returns:** `{ "is_duplicate": false, "match_id": null, "similarity": 0.0 }`

---

#### `run_deduplication`
**Rate:** light | **Caller:** internal

Full history scan for duplicates with optional auto-delete.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `auto_delete` | bool | false | Delete detected duplicates |
| `threshold` | float | 0.85 | Similarity threshold |

**Returns:** `{ "scanned", "duplicates_found", "deleted" }`

---

#### `get_dedup_stats`
**Rate:** light | **Caller:** internal

Deduplicator statistics.

**Params:** none

**Returns:** `{ "checked", "duplicates_found", "chars_saved" }`

---

#### `set_annotation`
**Rate:** light | **Caller:** internal

Save a user note (annotation) on a history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |
| `annotation` | string | yes | Note text |

**Returns:** `{ "ok": true }`

---

#### `get_annotation`
**Rate:** light | **Caller:** internal

Retrieve annotation for a history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "annotation": "..." }`

---

#### `search_annotations`
**Rate:** medium | **Caller:** internal

Full-text search across all annotations.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Query string |

**Returns:** `{ "items": [...] }`

---

#### `filter_by_confidence`
**Rate:** medium | **Caller:** internal

Filter history items by STT confidence score.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `min_confidence` | float | yes | Minimum confidence (0–1) |
| `max_confidence` | float | no | Maximum confidence |

**Returns:** `{ "items": [...] }`

---

### 4.4 Tags

---

#### `add_tag`
**Rate:** light | **Caller:** internal

Add a tag to a history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |
| `tag` | string | yes | Tag string |

**Returns:** `{ "tags": ["tag1", "tag2"] }`

---

#### `remove_tag`
**Rate:** light | **Caller:** internal

Remove a tag from a history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |
| `tag` | string | yes | Tag string |

**Returns:** `{ "tags": ["remaining"] }`

---

#### `get_tags`
**Rate:** light | **Caller:** internal

Get all tags for a history item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "tags": ["tag1"] }`

---

#### `search_by_tag`
**Rate:** medium | **Caller:** internal

Find history items matching a tag.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `tag` | string | yes | Tag to search |

**Returns:** `{ "items": [...] }`

---

#### `list_all_tags`
**Rate:** light | **Caller:** internal

All distinct tags used across history.

**Params:** none

**Returns:** `{ "tags": ["meeting", "call", ...] }`

---

### 4.5 Export

---

#### `export_history`
**Rate:** heavy | **Caller:** internal

Full history export to JSON file.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | string | auto | Output file path |
| `days` | int | null | Restrict to last N days |

**Returns:** `{ "path", "items_exported" }`

---

#### `export_history_srt`
**Rate:** heavy | **Caller:** internal

Export history as SubRip subtitle file.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Output .srt path |
| `history_ids` | string[] | no | Specific items (default all) |

**Returns:** `{ "path", "items_exported" }`

---

#### `export_history_csv`
**Rate:** heavy | **Caller:** internal

Export history as CSV file.

**Params:** same pattern as `export_history`

**Returns:** `{ "path", "items_exported" }`

---

#### `export_history_json`
**Rate:** heavy | **Caller:** internal

Export history as pretty-printed JSON.

**Params:** same pattern as `export_history`

**Returns:** `{ "path", "items_exported" }`

---

#### `export_history_markdown`
**Rate:** heavy | **Caller:** internal

Export history as Markdown document.

**Params:** same pattern as `export_history`

**Returns:** `{ "path", "items_exported" }`

---

#### `export_obsidian`
**Rate:** heavy | **Caller:** internal

Export history items as Obsidian-compatible `.md` files with YAML frontmatter.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `vault_path` | string | yes | Obsidian vault path |

**Returns:** `{ "exported", "skipped", "errors" }`

---

#### `export_history_json`
**Rate:** heavy | **Caller:** internal

Export history as JSON (canonical format).

**Params:** same as `export_history`

**Returns:** `{ "path", "items_exported" }`

---

#### `batch_export`
**Rate:** heavy | **Caller:** internal

Export history in multiple formats simultaneously.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `formats` | string[] | yes | Formats: `"json"`, `"csv"`, `"srt"`, `"markdown"` |
| `output_dir` | string | yes | Directory for output files |

**Returns:** `{ "files": { "json": "...", "csv": "..." } }`

---

#### `export_html_report` / `generate_html_report`
**Rate:** light | **Caller:** internal / Swift Analytics Dashboard

Generate a standalone HTML analytics report.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Report period |
| `path` | string | auto | Output file path |

**Returns:** `{ "path" }`

---

#### `export_settings`
**Rate:** light | **Caller:** internal

Export current settings to a JSON file.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Destination path |

**Returns:** `{ "path" }`

---

#### `import_settings`
**Rate:** light | **Caller:** internal

Import settings from a JSON file.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Source path |

**Returns:** `{ "imported_keys": [...] }`

---

### 4.6 Auto-backup & Auto-export

---

#### `get_auto_backup_status`
**Rate:** light | **Caller:** internal

Status of the auto-backup manager (last backup time, interval, copy count).

**Params:** none

**Returns:** `{ "enabled", "interval_hours", "last_backup_at", "backup_count" }`

---

#### `configure_auto_export`
**Rate:** light | **Caller:** internal

Set or update the auto-export schedule.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `enabled` | bool | yes | Enable/disable |
| `interval_hours` | int | no | Export interval |
| `format` | string | no | `"json"`, `"csv"`, `"markdown"` |
| `output_dir` | string | no | Destination directory |

**Returns:** `{ "ok": true }`

---

#### `get_export_schedule_status`
**Rate:** light | **Caller:** internal

Status of the export scheduler.

**Params:** none

**Returns:** `{ "enabled", "interval_hours", "last_export_at", "next_export_at" }`

---

#### `list_auto_exports`
**Rate:** light | **Caller:** internal

List all auto-export output files.

**Params:** none

**Returns:** `{ "exports": [ { "path", "created_at", "size_bytes" } ] }`

---

### 4.7 Collections & Chains

---

#### `create_collection`
**Rate:** light | **Caller:** internal

Create a named collection for organising history items.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Collection name |
| `description` | string | no | Optional description |

**Returns:** `{ "collection_id" }`

---

#### `delete_collection`
**Rate:** light | **Caller:** internal

Delete a collection (items are not deleted).

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `collection_id` | string | yes | Collection UUID |

**Returns:** `{ "deleted": true }`

---

#### `list_collections`
**Rate:** light | **Caller:** internal

List all collections.

**Params:** none

**Returns:** `{ "collections": [ { "collection_id", "name", "item_count" } ] }`

---

#### `add_to_collection`
**Rate:** light | **Caller:** internal

Add a history item to a collection.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `collection_id` | string | yes | Collection UUID |
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "ok": true }`

---

#### `remove_from_collection`
**Rate:** light | **Caller:** internal

Remove a history item from a collection.

**Params:** same as `add_to_collection`

**Returns:** `{ "ok": true }`

---

#### `get_collection_items`
**Rate:** light | **Caller:** internal

Retrieve items in a collection.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `collection_id` | string | yes | Collection UUID |

**Returns:** `{ "items": [...] }`

---

#### `start_chain`
**Rate:** light | **Caller:** internal

Begin a chain of related recording sessions.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | no | Chain name |

**Returns:** `{ "chain_id" }`

---

#### `add_to_chain`
**Rate:** light | **Caller:** internal

Add a history item to a recording chain.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `chain_id` | string | yes | Chain UUID |
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "ok": true }`

---

#### `end_chain`
**Rate:** light | **Caller:** internal

Mark a recording chain as complete.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `chain_id` | string | yes | Chain UUID |

**Returns:** `{ "ok": true }`

---

#### `get_chain`
**Rate:** light | **Caller:** internal

Get chain details including all member items.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `chain_id` | string | yes | Chain UUID |

**Returns:** `{ "chain_id", "name", "items": [...], "status" }`

---

#### `list_chains`
**Rate:** light | **Caller:** internal

List all recording chains.

**Params:** none

**Returns:** `{ "chains": [...] }`

---

#### `merge_chain_text`
**Rate:** light | **Caller:** internal

Merge all items in a chain into a single text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `chain_id` | string | yes | Chain UUID |

**Returns:** `{ "text": "merged transcript" }`

---

#### `merge_recordings`
**Rate:** light | **Caller:** internal

Merge multiple history items into one new item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_ids` | string[] | yes | Items to merge (in order) |

**Returns:** `{ "history_id": "new-uuid", "text": "..." }`

---

#### `preview_merge`
**Rate:** light | **Caller:** internal

Preview merge result without saving.

**Params:** same as `merge_recordings`

**Returns:** `{ "text": "..." }`

---

### 4.8 Archive

---

#### `archive_items`
**Rate:** light | **Caller:** internal

Move history items to archive.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_ids` | string[] | yes | Item UUIDs |

**Returns:** `{ "archived" }`

---

#### `unarchive_items`
**Rate:** light | **Caller:** internal

Restore items from archive.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_ids` | string[] | yes | Item UUIDs |

**Returns:** `{ "unarchived" }`

---

#### `list_archived`
**Rate:** light | **Caller:** internal

List all archived items.

**Params:** none

**Returns:** `{ "items": [...] }`

---

#### `get_archive_stats`
**Rate:** light | **Caller:** internal

Archive statistics.

**Params:** none

**Returns:** `{ "count", "size_bytes", "oldest_at", "newest_at" }`

---

### 4.9 Versioning & Sharing

---

#### `save_transcript_version`
**Rate:** light | **Caller:** internal

Save a new version of transcript text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |
| `text` | string | yes | New text version |

**Returns:** `{ "version_id" }`

---

#### `get_transcript_versions`
**Rate:** light | **Caller:** internal

Get all versions for a transcript.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "versions": [ { "version_id", "text", "saved_at" } ] }`

---

#### `revert_transcript_version`
**Rate:** light | **Caller:** internal

Revert transcript to a specific version.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |
| `version_id` | string | yes | Version UUID |

**Returns:** `{ "ok": true }`

---

#### `prepare_share`
**Rate:** light | **Caller:** internal

Prepare a shareable package for one or more transcripts.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_ids` | string[] | yes | Items to share |
| `include_audio` | bool | no | Attach audio files |

**Returns:** `{ "share_id", "path", "expires_at" }`

---

#### `list_shared`
**Rate:** light | **Caller:** internal

List all created share packages.

**Params:** none

**Returns:** `{ "packages": [ { "share_id", "created_at", "item_count" } ] }`

---

#### `get_shared`
**Rate:** light | **Caller:** internal

Retrieve a share package by ID.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `share_id` | string | yes | Share package UUID |

**Returns:** `{ "share_id", "items": [...], "created_at" }`

---

### 4.10 Translation

---

#### `translate_text`
**Rate:** medium | **Caller:** Swift main, HistoryPanel

Translate text using offline translator.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Source text |
| `mode` | string | no | Translation mode |
| `style` | string | no | `"neutral"`, `"formal"`, `"casual"` |

**Returns:**

```json
{
  "translated": "...",
  "source_lang": "ru",
  "target_lang": "es",
  "from_cache": false
}
```

---

#### `translate_selection`
**Rate:** excluded | **Caller:** internal (Phase 2A)

Translate selected text; called frequently during text selection.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Selected text |

**Returns:** same as `translate_text`

---

#### `set_translation_glossary_item`
**Rate:** light | **Caller:** Swift HistoryPanel

Add or update a glossary entry.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | yes | Source phrase |
| `target` | string | yes | Target phrase |

**Returns:** `{ "ok": true }`

---

#### `remove_translation_glossary_item`
**Rate:** light | **Caller:** Swift HistoryPanel

Remove a glossary entry.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | yes | Source phrase to remove |

**Returns:** `{ "ok": true }`

---

#### `get_glossary_suggestions`
**Rate:** medium | **Caller:** internal

Suggest new glossary entries based on translation history.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max suggestions |

**Returns:** `{ "suggestions": [ { "source", "target", "frequency" } ] }`

---

#### `suggest_medical_glossary_terms`
**Rate:** light | **Caller:** internal

Medical domain auto-learn: suggest ES↔RU pairs from translation history.

**Params:** none

**Returns:** `{ "suggestions": [...] }`

---

#### `apply_glossary_suggestions`
**Rate:** light | **Caller:** internal

Apply selected medical glossary suggestions to `translation_glossary`.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `suggestions` | object[] | yes | List of `{source, target}` to apply |

**Returns:** `{ "applied" }`

---

#### `get_vocabulary_suggestions`
**Rate:** medium | **Caller:** internal

Suggest STT vocabulary entries from recent transcription history.

**Params:** none

**Returns:** `{ "suggestions": ["word1", "word2"] }`

---

### 4.11 Settings

---

#### `get_settings`
**Rate:** excluded | **Caller:** Swift main

Get all current runtime settings.

**Params:** none

**Returns:** Full settings dict (see `DEFAULT_SETTINGS` in `core/config.py`).

---

#### `set_settings`
**Rate:** excluded | **Caller:** Swift main

Update one or more settings.

**Params:** Partial settings dict (any subset of keys from `DEFAULT_SETTINGS`).

**Returns:** `{ "updated_keys": ["key1"] }`

---

#### `apply_profile_preset`
**Rate:** excluded | **Caller:** internal

Apply a built-in profile preset (`default`, `meeting`, `translation`, `call_recording`).

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `preset` | string | yes | Preset name |

**Returns:** `{ "applied_keys": {...} }`

---

#### `list_profile_presets`
**Rate:** excluded | **Caller:** internal

List available profile presets with names and descriptions.

**Params:** none

**Returns:** `{ "presets": [ { "name", "description" } ] }`

---

#### `get_notification_preferences`
**Rate:** light | **Caller:** internal

Get notification settings.

**Params:** none

**Returns:** `{ "on_recording_complete", "on_low_confidence", "on_error" }`

---

#### `set_notification_preferences`
**Rate:** light | **Caller:** internal

Update notification settings.

**Params:** partial notification preferences dict

**Returns:** `{ "ok": true }`

---

#### `list_config_presets`
**Rate:** light | **Caller:** internal

List all configuration presets (built-in and custom).

**Params:** none

**Returns:** `{ "presets": [ { "name", "description", "is_builtin" } ] }`

---

#### `apply_config_preset`
**Rate:** light | **Caller:** internal

Apply a config preset and return the resulting settings patch.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Preset name |

**Returns:** `{ "settings_patch": {...} }`

---

#### `create_config_preset`
**Rate:** light | **Caller:** internal

Create a custom config preset from current settings.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Preset name |
| `description` | string | no | Description |
| `settings` | object | no | Settings snapshot (default: current) |

**Returns:** `{ "ok": true }`

---

#### `list_normalization_profiles`
**Rate:** light | **Caller:** internal

List available text normalisation profiles.

**Params:** none

**Returns:** `{ "profiles": [ { "name", "description" } ] }`

---

#### `apply_normalization_profile`
**Rate:** light | **Caller:** internal

Apply a normalisation profile to text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |
| `profile` | string | yes | Profile name |

**Returns:** `{ "text": "normalised output" }`

---

### 4.12 Summarisation & LLM

---

#### `summarize_text`
**Rate:** heavy | **Caller:** Swift HistoryPanel

LLM summarisation of raw text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Text to summarise |
| `max_sentences` | int | no | Target summary length |

**Returns:** `{ "summary": "...", "model": "qwen3-4b" }`

---

#### `summarize_item`
**Rate:** heavy | **Caller:** internal

LLM summarisation for a history item by ID.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "summary": "...", "history_id" }`

---

#### `get_last_llm_diff`
**Rate:** light | **Caller:** internal

Return the last word-level diff produced by the LLM rewriter.

**Params:** none

**Returns:** `{ "diff": [ { "type": "equal|replace|insert|delete", "old": "...", "new": "..." } ] }`

---

#### `auto_summarize_batch`
**Rate:** heavy | **Caller:** internal

Batch LLM summarisation for multiple history items.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_ids` | string[] | no | Items to summarise (default: unsummarised) |
| `profile` | string | no | Summary profile name |

**Returns:** `{ "summarised", "failed", "results": [...] }`

---

#### `list_summary_profiles`
**Rate:** light | **Caller:** internal

List custom summarisation profiles.

**Params:** none

**Returns:** `{ "profiles": [ { "name", "system_prompt" } ] }`

---

#### `add_summary_profile`
**Rate:** light | **Caller:** internal

Add a custom summarisation profile.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Profile name |
| `system_prompt` | string | yes | LLM system prompt |

**Returns:** `{ "ok": true }`

---

### 4.13 Diagnostics & Health

---

#### `get_diagnostics`
**Rate:** medium | **Caller:** internal

Structured diagnostics snapshot: system, STT, LLM, history, settings cache.

**Params:** none

**Returns:**

```json
{
  "system": { "platform", "python_version", "memory_mb" },
  "stt": { "model_loaded", "unavailable_models": [] },
  "llm": { "rewriter_ok", "circuit_open" },
  "history": { "item_count", "file_size_bytes" },
  "settings_cache": { "cached", "age_sec" }
}
```

---

#### `health_check`
**Rate:** medium | **Caller:** internal

Aggregate health check across all subsystems.

**Params:** none

**Returns:** `{ "healthy": true, "components": { "recorder": "ok", "stt": "ok", "llm": "degraded", ... } }`

---

#### `get_startup_diagnostics`
**Rate:** light | **Caller:** internal

Run all startup readiness checks and report status.

**Params:** none

**Returns:** `{ "checks": [ { "name", "status", "message" } ] }`

---

#### `get_error_report`
**Rate:** medium | **Caller:** internal

Recent errors from the ring-buffer error reporter.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max errors |

**Returns:** `{ "errors": [ { "ts", "component", "type", "message" } ] }`

---

#### `get_error_stats`
**Rate:** medium | **Caller:** internal

Error counts grouped by component and type.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `window_sec` | int | 3600 | Time window |

**Returns:** `{ "by_component": {...}, "by_type": {...}, "total" }`

---

#### `get_metrics_dashboard`
**Rate:** medium | **Caller:** internal

Real-time metrics snapshot from `MetricsCollector`.

**Params:** none

**Returns:** `{ "latency_p50_ms", "latency_p95_ms", "confidence_avg", "diarization_rate", "recording_count" }`

---

#### `get_system_info`
**Rate:** medium | **Caller:** internal

Real-time system resource monitoring.

**Params:** none

**Returns:** `{ "cpu_pct", "ram_used_mb", "ram_total_mb", "disk_free_gb", "gpu_vram_mb" }`

---

#### `get_throttle_stats`
**Rate:** light | **Caller:** internal (breadcrumb-excluded)

IPC throttle statistics.

**Params:** none

**Returns:** `{ "methods": { "<method>": { "calls", "rejected", "bucket_tokens" } } }`

---

### 4.14 Analytics & Trends

---

#### `get_analytics_dashboard`
**Rate:** light | **Caller:** internal

Comprehensive analytics dashboard: all metrics in one call.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** Combined dict from all analytics subsystems.

---

#### `generate_daily_digest`
**Rate:** heavy | **Caller:** internal

Generate daily digest summary of transcription activity.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `date` | string | today | ISO date string |

**Returns:** `{ "date", "total_recordings", "total_words", "top_topics": [], "summary": "..." }`

---

#### `analyze_quality_trends`
**Rate:** heavy | **Caller:** internal

Quality trend analysis over time.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** `{ "trend": "improving|stable|declining", "daily": [ { "date", "avg_confidence" } ] }`

---

#### `get_activity_calendar`
**Rate:** light | **Caller:** internal

GitHub-style activity calendar data.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 365 | Calendar period |

**Returns:** `{ "days": [ { "date", "count", "level": 0-4 } ] }`

---

#### `get_speaker_statistics`
**Rate:** light | **Caller:** internal

Per-speaker word count, duration, confidence from diarized history.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** `{ "speakers": [ { "label", "word_count", "duration_sec", "confidence" } ] }`

---

#### `get_sentiment_trends`
**Rate:** light | **Caller:** internal

Sentiment trend analysis over transcriptions.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** `{ "trend": "improving|stable|declining", "daily": [ { "date", "sentiment" } ] }`

---

#### `compare_periods`
**Rate:** heavy | **Caller:** internal

Compare transcription statistics across two arbitrary time periods.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `period_a_start` | string | yes | ISO date start |
| `period_a_end` | string | yes | ISO date end |
| `period_b_start` | string | yes | ISO date start |
| `period_b_end` | string | yes | ISO date end |

**Returns:** `{ "period_a": {...stats}, "period_b": {...stats}, "delta": {...} }`

---

#### `get_usage_stats`
**Rate:** medium | **Caller:** internal

Daily usage statistics (recordings, duration, words).

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** `{ "daily": [ { "date", "recordings", "duration_sec", "words" } ] }`

---

#### `get_topic_timeline`
**Rate:** light | **Caller:** internal

Timeline of topic shifts across recent transcriptions.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 7 | Lookback window |

**Returns:** `{ "timeline": [ { "ts", "topic", "text_snippet" } ] }`

---

#### `get_timeline_view`
**Rate:** light | **Caller:** internal

Group history by time blocks (hour/day/week).

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `granularity` | string | `"day"` | `"hour"`, `"day"`, `"week"` |
| `days` | int | 30 | Period |

**Returns:** `{ "blocks": [ { "period", "items": [...] } ] }`

---

#### `export_timeline`
**Rate:** light | **Caller:** internal

Export timeline in SVG, JSON or iCal format.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `format` | string | yes | `"svg"`, `"json"`, `"ical"` |
| `path` | string | yes | Output path |

**Returns:** `{ "path" }`

---

#### `generate_stats_report`
**Rate:** light | **Caller:** internal

Generate a full Markdown statistics report for a period.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Report period |

**Returns:** `{ "report": "# Markdown report..." }`

---

#### `generate_mini_stats_report`
**Rate:** light | **Caller:** internal

Generate a concise 5-line status report.

**Params:** none

**Returns:** `{ "report": "brief text" }`

---

#### `get_keyword_cloud`
**Rate:** light | **Caller:** internal

Word-cloud data (count, weight, font_size) for visualisation.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |
| `top_n` | int | 50 | Number of words |

**Returns:** `{ "words": [ { "word", "count", "weight", "font_size" } ] }`

---

### 4.15 Text Processing

---

#### `detect_language`
**Rate:** light | **Caller:** internal

Heuristic language detection (RU/ES/EN).

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |

**Returns:** `{ "language": "ru", "confidence": 0.95 }`

---

#### `extract_terms`
**Rate:** medium | **Caller:** internal

Keyword/term extraction from transcript text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |
| `top_n` | int | no | Max terms |

**Returns:** `{ "terms": [ { "term", "score" } ] }`

---

#### `compare_texts`
**Rate:** medium | **Caller:** internal

Structural diff and similarity scoring between two texts.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text_a` | string | yes | First text |
| `text_b` | string | yes | Second text |

**Returns:** `{ "similarity": 0.72, "diff": [ { "type", "text" } ] }`

---

#### `score_readability`
**Rate:** light | **Caller:** internal

Flesch readability score and sentence/vocabulary complexity.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |

**Returns:** `{ "flesch_score", "grade_level", "avg_sentence_len", "complex_word_ratio" }`

---

#### `score_transcription`
**Rate:** light | **Caller:** internal

Composite quality score 0–100 (grade A–F) from confidence, duration, diarization, LLM flags.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | no | Score from stored item |
| `text` | string | no | Or score from raw text |
| `confidence` | float | no | STT confidence |

**Returns:** `{ "score": 84, "grade": "B", "breakdown": {...} }`

---

#### `detect_emotion`
**Rate:** light | **Caller:** internal

Heuristic emotion detection in transcript text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |

**Returns:** `{ "emotion": "neutral|positive|negative|anxious|excited", "confidence": 0.7 }`

---

#### `anonymize_text`
**Rate:** light | **Caller:** internal

PII redaction (phone numbers, emails, credit cards, etc.).

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |

**Returns:** `{ "text": "anonymised text", "redacted_count" }`

---

#### `generate_auto_title`
**Rate:** light | **Caller:** internal

Heuristic auto-title generation from transcript text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Transcript text |

**Returns:** `{ "title": "generated title" }`

---

#### `post_process_text`
**Rate:** light | **Caller:** internal

Run text through configurable post-processing pipeline.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |
| `steps` | string[] | no | Pipeline steps to apply |

**Returns:** `{ "text": "processed" }`

---

#### `list_post_process_steps`
**Rate:** light | **Caller:** internal

List available post-processing step names.

**Params:** none

**Returns:** `{ "steps": ["whitespace", "punctuation", "entities", "abbreviations", "anonymize"] }`

---

#### `format_for_paste`
**Rate:** light | **Caller:** internal

Format transcript for a target application.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Transcript text |
| `target` | string | yes | App target: `"telegram"`, `"notes"`, `"email"`, etc. |

**Returns:** `{ "text": "formatted output" }`

---

#### `list_paste_formatters`
**Rate:** light | **Caller:** internal

List all available paste formatter targets.

**Params:** none

**Returns:** `{ "formatters": [ { "name", "description" } ] }`

---

#### `expand_abbreviations`
**Rate:** light | **Caller:** internal

Expand abbreviations in transcript text.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Input text |
| `language` | string | no | `"ru"`, `"es"`, `"en"` |

**Returns:** `{ "text": "expanded text" }`

---

#### `add_abbreviation`
**Rate:** light | **Caller:** internal

Add a user-defined abbreviation expansion.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `abbreviation` | string | yes | Short form |
| `expansion` | string | yes | Full form |
| `language` | string | yes | Language code |

**Returns:** `{ "ok": true }`

---

#### `remove_abbreviation`
**Rate:** light | **Caller:** internal

Remove an abbreviation entry.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `abbreviation` | string | yes | Short form |
| `language` | string | yes | Language code |

**Returns:** `{ "ok": true }`

---

#### `list_abbreviations`
**Rate:** light | **Caller:** internal

List abbreviations for a language.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `language` | string | yes | Language code |

**Returns:** `{ "abbreviations": { "RU": "Русский" } }`

---

#### `compare_recordings`
**Rate:** light | **Caller:** internal

Side-by-side multi-recording comparison: similarity matrix, shared/unique words.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_ids` | string[] | yes | 2+ item UUIDs |

**Returns:** `{ "matrix": [[1.0, 0.72], [0.72, 1.0]], "shared_words": [...], "unique_words": {...} }`

---

#### `get_context_memory`
**Rate:** light | **Caller:** internal

STT context memory: recent words and topics from last N transcriptions.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 5 | Number of recent items |

**Returns:** `{ "words": [...], "topics": [...] }`

---

### 4.16 Hallucination Management

---

#### `add_hallucination_pattern`
**Rate:** light | **Caller:** internal

Add a user-defined hallucination pattern for STT cleanup.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `pattern` | string | yes | Regex or literal string |

**Returns:** `{ "ok": true }`

---

#### `remove_hallucination_pattern`
**Rate:** light | **Caller:** internal

Remove a hallucination pattern.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `pattern` | string | yes | Pattern to remove |

**Returns:** `{ "ok": true }`

---

#### `list_hallucination_patterns`
**Rate:** light | **Caller:** internal

Get all hallucination patterns (built-in + user-defined).

**Params:** none

**Returns:** `{ "builtin": [...], "custom": [...] }`

---

### 4.17 Scheduling

---

#### `schedule_recording`
**Rate:** light | **Caller:** internal

Schedule a future recording with start time and optional duration.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `start_at` | string | yes | ISO datetime |
| `duration_sec` | int | no | Max recording duration |

**Returns:** `{ "schedule_id" }`

---

#### `cancel_scheduled_recording`
**Rate:** light | **Caller:** internal

Cancel a scheduled recording.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `schedule_id` | string | yes | Schedule UUID |

**Returns:** `{ "cancelled": true }`

---

#### `list_scheduled_recordings`
**Rate:** light | **Caller:** internal

List all pending scheduled recordings.

**Params:** none

**Returns:** `{ "schedules": [ { "schedule_id", "start_at", "duration_sec", "status" } ] }`

---

### 4.18 Transcription Queue

---

#### `enqueue_transcription`
**Rate:** light | **Caller:** internal

Add an audio file to the transcription queue with priority.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Audio file path |
| `priority` | int | no | Priority 1 (highest)–10 (lowest) |

**Returns:** `{ "job_id" }`

---

#### `cancel_transcription`
**Rate:** light | **Caller:** internal

Cancel a queued transcription job.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | Job UUID |

**Returns:** `{ "cancelled": true }`

---

#### `get_queue_status`
**Rate:** light | **Caller:** internal

Status of a single queue job.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | Job UUID |

**Returns:** `{ "job_id", "status", "progress", "result" }`

---

#### `list_transcription_queue`
**Rate:** light | **Caller:** internal

All jobs in the transcription queue.

**Params:** none

**Returns:** `{ "jobs": [ { "job_id", "status", "path", "priority", "enqueued_at" } ] }`

---

### 4.19 Speakers

---

#### `set_speaker_alias`
**Rate:** light | **Caller:** internal

Assign a human-readable alias to a diarization speaker label.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `speaker` | string | yes | e.g. `"SPEAKER_00"` |
| `alias` | string | yes | Human name |

**Returns:** `{ "ok": true }`

---

#### `get_speaker_aliases`
**Rate:** light | **Caller:** internal

Get all speaker label → alias mappings.

**Params:** none

**Returns:** `{ "aliases": { "SPEAKER_00": "Pablo", "SPEAKER_01": "Maria" } }`

---

#### `remove_speaker_alias`
**Rate:** light | **Caller:** internal

Remove a speaker alias.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `speaker` | string | yes | Speaker label |

**Returns:** `{ "ok": true }`

---

### 4.20 Vocabulary & Smart Vocab

---

#### `auto_update_vocabulary`
**Rate:** light | **Caller:** internal

Auto-update STT vocabulary from recent transcription history.

**Params:** none

**Returns:** `{ "added": ["word1"], "removed": [] }`

---

#### `get_smart_vocabulary_suggestions`
**Rate:** light | **Caller:** internal

Vocabulary suggestions based on usage patterns.

**Params:** none

**Returns:** `{ "suggestions": [ { "word", "frequency", "context" } ] }`

---

#### `select_model`
**Rate:** light | **Caller:** internal

Smart STT model selection based on recording conditions.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `duration_sec` | float | yes | Expected duration |
| `noise_level` | float | no | Estimated noise level |

**Returns:** `{ "model": "balanced", "reason": "..." }`

---

### 4.21 Data Integrity & Migration

---

#### `check_integrity`
**Rate:** heavy | **Caller:** internal

Validate NDJSON history file integrity.

**Params:** none

**Returns:** `{ "valid": true, "issues": [], "checked_items" }`

---

#### `repair_integrity`
**Rate:** heavy | **Caller:** internal

Repair integrity issues in history data.

**Params:** none

**Returns:** `{ "repaired", "items_removed", "backup_path" }`

---

#### `check_migration`
**Rate:** light | **Caller:** internal

Check whether a data schema migration is needed.

**Params:** none

**Returns:** `{ "needs_migration": false, "current_version", "target_version" }`

---

#### `run_migration`
**Rate:** light | **Caller:** internal

Execute data migration to target schema version.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `target_version` | int | latest | Target schema version |

**Returns:** `{ "migrated": true, "from_version", "to_version" }`

---

### 4.22 Event System

---

#### `get_event_log`
**Rate:** medium | **Caller:** internal (breadcrumb-excluded)

Event log entries for debugging (filterable by type/time).

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `event_type` | string | null | Filter by event type |
| `since` | float | null | Unix timestamp start |
| `limit` | int | 100 | Max entries |

**Returns:** `{ "events": [ { "type", "ts", "data" } ] }`

---

#### `get_event_stats`
**Rate:** medium | **Caller:** internal (breadcrumb-excluded)

Event statistics: counters and rate per minute.

**Params:** none

**Returns:** `{ "by_type": {...}, "total", "rate_per_min" }`

---

#### `replay_events`
**Rate:** medium | **Caller:** internal

Replay events in a time range.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `since` | float | yes | Unix timestamp start |
| `until` | float | yes | Unix timestamp end |

**Returns:** `{ "replayed", "events": [...] }`

---

### 4.23 Search History

---

#### `get_recent_searches`
**Rate:** light | **Caller:** internal

Last N search queries from search history.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max entries |

**Returns:** `{ "searches": [ { "query", "ts" } ] }`

---

#### `get_popular_searches`
**Rate:** light | **Caller:** internal

Most frequent search queries.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 10 | Top N |

**Returns:** `{ "searches": [ { "query", "count" } ] }`

---

#### `clear_search_history`
**Rate:** light | **Caller:** internal

Clear all search history.

**Params:** none

**Returns:** `{ "cleared": true }`

---

### 4.24 Obsidian Sync

---

#### `configure_obsidian_sync`
**Rate:** light | **Caller:** internal

Configure Obsidian vault path and sync settings.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `vault_path` | string | yes | Absolute path to Obsidian vault |
| `folder` | string | no | Sub-folder in vault (default `"KrabEar"`) |
| `enabled` | bool | no | Enable sync |

**Returns:** `{ "ok": true }`

---

#### `run_obsidian_sync`
**Rate:** light | **Caller:** internal

Sync history items to Obsidian vault (incremental by default).

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `force` | bool | false | Force full re-sync |

**Returns:** `{ "synced", "skipped", "errors": [] }`

---

#### `get_obsidian_sync_status`
**Rate:** light | **Caller:** internal

Status of the last Obsidian sync.

**Params:** none

**Returns:** `{ "enabled", "last_sync_at", "vault_path", "synced_count" }`

---

### 4.25 Playback Tracking

---

#### `record_playback`
**Rate:** light | **Caller:** internal

Register a playback event.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |
| `duration_listened_sec` | float | yes | How long user listened |

**Returns:** `{ "ok": true }`

---

#### `get_playback_stats`
**Rate:** light | **Caller:** internal

Playback stats for one item.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "play_count", "total_listened_sec", "last_played_at" }`

---

#### `get_most_replayed`
**Rate:** light | **Caller:** internal

Top N most-replayed items.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 10 | Top N |

**Returns:** `{ "items": [ { "history_id", "play_count", "total_listened_sec" } ] }`

---

### 4.26 Templates & Webhooks

---

#### `get_templates`
**Rate:** light | **Caller:** internal

List quick-insert text templates.

**Params:** none

**Returns:** `{ "templates": [ { "name", "text" } ] }`

---

#### `add_template`
**Rate:** light | **Caller:** internal

Add a new text template.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Template name |
| `text` | string | yes | Template body (supports `{{variables}}`) |

**Returns:** `{ "ok": true }`

---

#### `remove_template`
**Rate:** light | **Caller:** internal

Remove a text template.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Template name |

**Returns:** `{ "ok": true }`

---

#### `apply_template`
**Rate:** light | **Caller:** internal

Apply a template, substituting variables.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Template name |
| `variables` | object | no | Variable substitutions |

**Returns:** `{ "text": "expanded template" }`

---

#### `register_webhook`
**Rate:** light | **Caller:** internal

Register an HTTP webhook for backend events.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | Webhook endpoint URL |
| `events` | string[] | yes | Event types to subscribe |
| `secret` | string | no | HMAC signing secret |

**Returns:** `{ "webhook_id" }`

---

#### `unregister_webhook`
**Rate:** light | **Caller:** internal

Remove a webhook registration.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `webhook_id` | string | yes | Webhook UUID |

**Returns:** `{ "removed": true }`

---

#### `list_webhooks`
**Rate:** light | **Caller:** internal

List all registered webhooks.

**Params:** none

**Returns:** `{ "webhooks": [ { "webhook_id", "url", "events": [] } ] }`

---

### 4.27 Feature Flags

---

#### `get_feature_flags`
**Rate:** light | **Caller:** internal

Get all feature flags with current values and descriptions.

**Params:** none

**Returns:** `{ "flags": { "flag_name": { "enabled": true, "description": "..." } } }`

---

#### `set_feature_flag`
**Rate:** light | **Caller:** internal

Set a feature flag value.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `flag` | string | yes | Flag name |
| `enabled` | bool | yes | New value |

**Returns:** `{ "ok": true }`

---

### 4.28 Hotword Detection

---

#### `add_hotword`
**Rate:** light | **Caller:** internal

Add a trigger word for hotword detection.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `word` | string | yes | Trigger word |

**Returns:** `{ "ok": true }`

---

#### `remove_hotword`
**Rate:** light | **Caller:** internal

Remove a trigger word.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `word` | string | yes | Trigger word |

**Returns:** `{ "ok": true }`

---

#### `get_hotwords`
**Rate:** light | **Caller:** internal

List all configured hotwords.

**Params:** none

**Returns:** `{ "hotwords": ["palabra", "слово"] }`

---

#### `check_hotwords`
**Rate:** light | **Caller:** internal

Check text for hotword matches.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Text to scan |

**Returns:** `{ "matches": [ { "word", "position" } ] }`

---

### 4.29 Model Cache

---

#### `list_cached_models`
**Rate:** light | **Caller:** internal

List all cached HuggingFace/MLX models.

**Params:** none

**Returns:** `{ "models": [ { "name", "size_mb", "last_used_at" } ] }`

---

#### `get_model_cache_info`
**Rate:** light | **Caller:** internal

Detailed cache info for a specific model.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `model_name` | string | yes | Model identifier |

**Returns:** `{ "model_name", "size_mb", "cache_path", "last_used_at" }`

---

### 4.30 Wake Word (openWakeWord)

---

#### `get_wake_word_config`
**Rate:** light | **Caller:** internal

Get wake word configuration status.

**Params:** none

**Returns:** `{ "enabled", "engine", "access_key_present", "ppn_present", "brain" }`

---

#### `set_wake_word_config`
**Rate:** light | **Caller:** internal

Update wake word settings.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `enabled` | bool | no | Enable/disable |
| `engine` | string | no | `"oww"` or `"picovoice"` |
| `brain` | string | no | Brain endpoint for wake activation |

**Returns:** `{ "ok": true }`

---

#### `wake_word_list_models`
**Rate:** light | **Caller:** internal

List built-in and custom openWakeWord models.

**Params:** none

**Returns:** `{ "models": [ { "name", "type": "builtin|custom", "path" } ] }`

---

#### `wake_word_start`
**Rate:** light | **Caller:** internal

Start wake word listening.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `model` | string | no | Model name (default: `"hey_jarvis"`) |

**Returns:** `{ "started": true }`

---

#### `wake_word_stop`
**Rate:** light | **Caller:** internal

Stop wake word listening.

**Params:** none

**Returns:** `{ "stopped": true }`

---

#### `wake_word_status`
**Rate:** light | **Caller:** internal

Status of the wake word adapter.

**Params:** none

**Returns:** `{ "running": false, "model", "detections_today" }`

---

### 4.31 TTS (Speech Synthesis)

---

#### `synthesize_speech`
**Rate:** light | **Caller:** internal

Dual-mode TTS: Silero (RU), Kokoro (EN), macOS `say` fallback.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Text to synthesise |
| `language` | string | no | `"ru"`, `"en"`, `"auto"` |
| `voice` | string | no | Voice name |

**Returns:** `{ "audio_path", "duration_sec", "engine_used" }`

---

### 4.32 Live Subtitles

---

#### `live_subs_ingest`
**Rate:** excluded | **Caller:** internal (Sprint 2B)

Streaming STT + translate for live subtitles. Called up to 30×/sec per audio chunk.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `audio_chunk` | number[] | yes | PCM audio samples (float32) |
| `session_id` | string | yes | Session UUID |

**Returns:** `{ "text": "partial", "final": false }`

---

#### `live_subs_stop`
**Rate:** excluded | **Caller:** internal

Flush live subs buffer and stop session.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Session UUID |

**Returns:** `{ "text": "final transcript" }`

---

### 4.33 Plugins

---

#### `list_plugins`
**Rate:** light | **Caller:** internal

List all discovered plugins.

**Params:** none

**Returns:** `{ "plugins": [ { "name", "version", "enabled" } ] }`

---

#### `get_plugin_info`
**Rate:** light | **Caller:** internal

Info about a specific plugin.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Plugin name |

**Returns:** `{ "name", "version", "description", "enabled", "hooks": [] }`

---

### 4.34 Cost Estimation

---

#### `estimate_recording_cost`
**Rate:** light | **Caller:** internal

Estimate compute cost for processing a recording.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `duration_sec` | float | yes | Audio duration |
| `quality_profile` | string | no | STT quality profile |

**Returns:** `{ "cpu_time_sec", "memory_mb", "disk_mb", "estimated_usd" }`

---

#### `get_daily_cost_summary`
**Rate:** light | **Caller:** internal

Today's compute cost summary.

**Params:** none

**Returns:** `{ "cpu_time_sec", "recordings", "total_estimated_usd" }`

---

### 4.35 Language Learning

---

#### `extract_learning_vocabulary`
**Rate:** light | **Caller:** internal

Extract bilingual vocabulary pairs from bilingual transcripts.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |

**Returns:** `{ "pairs": [ { "source", "target", "context" } ] }`

---

#### `generate_flashcards`
**Rate:** light | **Caller:** internal

Generate flashcard data from bilingual vocabulary.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback window |
| `limit` | int | 20 | Max flashcards |

**Returns:** `{ "cards": [ { "front", "back", "context" } ] }`

---

#### `get_learning_stats`
**Rate:** light | **Caller:** internal

Language learning progress statistics.

**Params:** none

**Returns:** `{ "vocabulary_size", "pairs_learned", "sessions" }`

---

### 4.36 Recording Enrichment

---

#### `enrich_recording`
**Rate:** light | **Caller:** internal

Auto-enrich recording metadata: word_count, emotion, pace, quality, topics.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `history_id` | string | yes | Item UUID |

**Returns:** `{ "enriched_fields": {...} }`

---

### 4.37 Call Assist

---

#### `start_call_assist`
**Rate:** light | **Caller:** Swift HistoryPanel

Start a real-time call assist session via Voice Gateway.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `language` | string | no | Session language |

**Returns:** `{ "session_id", "status": "started" }`

---

#### `stop_call_assist`
**Rate:** light | **Caller:** Swift HistoryPanel

Stop the active call assist session.

**Params:** none

**Returns:** `{ "stopped": true }`

---

#### `get_call_assist_state`
**Rate:** light | **Caller:** Swift HistoryPanel (breadcrumb-excluded)

Get current call assist state.

**Params:** none

**Returns:** `{ "active": false, "session_id": null, "duration_sec": 0 }`

---

#### `call_assist_diagnostics`
**Rate:** light | **Caller:** Swift HistoryPanel

Diagnostic snapshot for call assist subsystem.

**Params:** none

**Returns:** `{ "gateway_connected", "session_active", "voice_gateway_url", ... }`

---

#### `call_assist_summary`
**Rate:** light | **Caller:** Swift HistoryPanel

AI summary of the current call session.

**Params:** none

**Returns:** `{ "summary": "..." }`

---

#### `call_assist_quick_phrase`
**Rate:** light | **Caller:** Swift HistoryPanel

Send a quick phrase to the call Gateway.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `phrase` | string | yes | Phrase to inject |

**Returns:** `{ "ok": true }`

---

#### `list_call_assist_quick_phrases`
**Rate:** light | **Caller:** Swift HistoryPanel

List preset quick phrases.

**Params:** none

**Returns:** `{ "phrases": ["Повторите", "Не понял", ...] }`

---

#### `call_assist_cost_estimate`
**Rate:** light | **Caller:** Swift HistoryPanel

Estimate cost of current call session.

**Params:** none

**Returns:** `{ "duration_sec", "estimated_usd" }`

---

#### `call_assist_timeline`
**Rate:** light | **Caller:** Swift HistoryPanel

Full timeline of events in the call session.

**Params:** none

**Returns:** `{ "events": [ { "ts", "type", "text" } ] }`

---

#### `call_assist_timeline_stats`
**Rate:** light | **Caller:** Swift HistoryPanel

Statistics from the call timeline.

**Params:** none

**Returns:** `{ "total_events", "speaking_time_sec", "topics": [] }`

---

#### `call_assist_timeline_summary`
**Rate:** light | **Caller:** Swift HistoryPanel

Summary generated from call timeline events.

**Params:** none

**Returns:** `{ "summary": "..." }`

---

#### `call_assist_timeline_export`
**Rate:** light | **Caller:** Swift HistoryPanel

Export call timeline to a file.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `format` | string | yes | `"json"`, `"markdown"`, `"srt"` |
| `path` | string | yes | Output path |

**Returns:** `{ "path" }`

---

#### `call_assist_timeline_clear`
**Rate:** light | **Caller:** Swift HistoryPanel

Clear the call timeline.

**Params:** none

**Returns:** `{ "cleared": true }`

---

#### `call_assist_timeline_to_history`
**Rate:** light | **Caller:** Swift HistoryPanel

Save call timeline as a history item.

**Params:** none

**Returns:** `{ "history_id" }`

---

#### `call_assist_list_templates`
**Rate:** light | **Caller:** internal

List call assist reply templates.

**Params:** none

**Returns:** `{ "templates": [ { "name", "text" } ] }`

---

#### `call_assist_add_template`
**Rate:** light | **Caller:** internal

Add a call assist reply template.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Template name |
| `text` | string | yes | Template text |

**Returns:** `{ "ok": true }`

---

#### `call_assist_remove_template`
**Rate:** light | **Caller:** internal

Remove a call assist reply template.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Template name |

**Returns:** `{ "ok": true }`

---

#### `call_assist_template`
**Rate:** light | **Caller:** internal

Send a template reply to the Voice Gateway.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Template name |

**Returns:** `{ "ok": true }`

---

#### `call_assist_cost_report`
**Rate:** light | **Caller:** internal

Detailed cost report for the current call session.

**Params:** none

**Returns:** `{ "duration_sec", "words_spoken", "translations", "llm_calls", "estimated_usd" }`

---

### 4.38 Call Session CRUD (Phase 3 Call Automation)

---

#### `call_session_create`
**Rate:** light | **Caller:** internal

Create an outbound call session.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `phone_number` | string | yes | Destination number (E.164) |
| `provider` | string | no | Telephony provider |
| `metadata` | object | no | Extra metadata |

**Returns:** `{ "session_id", "status": "created" }`

---

#### `call_session_get`
**Rate:** light | **Caller:** internal

Get call session by ID.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Session UUID |

**Returns:** `{ "session_id", "status", "phone_number", "started_at", "duration_sec", ... }`

---

#### `call_session_list`
**Rate:** light | **Caller:** internal

List call sessions with optional status filter.

**Params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `status` | string | null | Filter: `"active"`, `"ended"`, `"failed"` |
| `limit` | int | 20 | Max results |

**Returns:** `{ "sessions": [...] }`

---

#### `call_session_update_status`
**Rate:** light | **Caller:** internal

Transition a call session status.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Session UUID |
| `status` | string | yes | New status |

**Returns:** `{ "ok": true }`

---

#### `call_session_add_transcript`
**Rate:** light | **Caller:** internal

Append a transcript line to a call session.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Session UUID |
| `speaker` | string | yes | `"agent"` or `"user"` |
| `text` | string | yes | Transcript text |

**Returns:** `{ "ok": true }`

---

#### `call_session_end`
**Rate:** light | **Caller:** internal

End a call session and compute final duration and cost.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Session UUID |

**Returns:** `{ "ok": true, "duration_sec", "total_cost_usd" }`

---

### 4.39 Phase 3 Safeguards

---

#### `call_estimate_cost`
**Rate:** excluded | **Caller:** internal (polling)

Estimate call cost by provider and destination country.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | string | yes | Telephony provider |
| `country_code` | string | yes | ISO 3166-1 alpha-2 |
| `duration_sec` | int | no | Estimated duration |

**Returns:** `{ "cost_per_min_usd", "estimated_total_usd" }`

---

#### `call_check_auto_end`
**Rate:** excluded | **Caller:** internal (polling)

Check auto-end rules for active call session.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Session UUID |

**Returns:** `{ "should_end": false, "reason": null }`

---

### 4.40 Telegram Bridge

---

#### `send_to_telegram`
**Rate:** light | **Caller:** internal

Send a transcription to Telegram via the main Krab userbot.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Text to send |
| `chat_id` | string | no | Target chat (default: saved messages) |
| `history_id` | string | no | Source history item |

**Returns:** `{ "sent": true, "message_id" }`

---

### 4.41 Batch Execution

---

#### `batch`
**Rate:** light | **Caller:** internal

Execute multiple IPC methods in a single call. Max 50 sub-requests.

**Params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `requests` | object[] | yes | List of `{ method, params? }` |

**Returns:**

```json
{
  "results": [
    { "method": "ping", "ok": true, "result": {...} },
    { "method": "get_settings", "ok": true, "result": {...} }
  ],
  "total": 2,
  "succeeded": 2,
  "failed": 0
}
```

A failure in one sub-request does not abort the rest.

---

## 5. Appendix A — Rate Limit Configuration

Rate limits can be overridden by passing a custom `limits` dict to `IPCThrottle`:

```python
IPCThrottle(limits={"heavy": 10, "medium": 60, "light": 300})
```

Default limits:

| Class | Default (calls/min) |
|-------|---------------------|
| heavy | 5 |
| medium | 30 |
| light | 120 |
| excluded | unlimited |

---

## 6. Appendix B — EXCLUDED Methods (no throttle, no Sentry breadcrumb)

Methods with no throttle AND no Sentry breadcrumb (very high-frequency):

- `ping`
- `get_recording_state`
- `get_call_assist_state`
- `live_subs_ingest`
- `get_throttle_stats`
- `get_event_log`
- `get_event_stats`

Methods with no throttle but WITH Sentry breadcrumb:

- `start_recording`
- `stop_recording`
- `set_paste_status`
- `get_settings`
- `set_settings`
- `apply_profile_preset`
- `list_profile_presets`
- `translate_selection`
- `live_subs_stop`
- `call_estimate_cost`
- `call_check_auto_end`

---

## 7. Appendix C — Method Index

| Method | Category | Rate |
|--------|----------|------|
| `add_abbreviation` | Text Processing | light |
| `add_hallucination_pattern` | Hallucination | light |
| `add_history_item` | History | light |
| `add_hotword` | Hotwords | light |
| `add_summary_profile` | LLM | light |
| `add_tag` | Tags | light |
| `add_template` | Templates | light |
| `add_to_chain` | Chains | light |
| `add_to_collection` | Collections | light |
| `analyze_audio_quality` | Audio | heavy |
| `analyze_quality_trends` | Analytics | heavy |
| `analyze_silence` | Audio | heavy |
| `analyze_speech_pace` | Text | light |
| `analyze_word_timing` | Audio | light |
| `anonymize_text` | Text | light |
| `apply_config_preset` | Settings | light |
| `apply_glossary_suggestions` | Translation | light |
| `apply_normalization_profile` | Settings | light |
| `apply_profile_preset` | Settings | excluded |
| `apply_template` | Templates | light |
| `archive_items` | Archive | light |
| `auto_summarize_batch` | LLM | heavy |
| `auto_update_vocabulary` | Vocabulary | light |
| `backup_history` | History | light |
| `batch` | Misc | light |
| `batch_export` | Export | heavy |
| `call_assist_add_template` | Call Assist | light |
| `call_assist_cost_estimate` | Call Assist | light |
| `call_assist_cost_report` | Call Assist | light |
| `call_assist_diagnostics` | Call Assist | light |
| `call_assist_list_templates` | Call Assist | light |
| `call_assist_quick_phrase` | Call Assist | light |
| `call_assist_remove_template` | Call Assist | light |
| `call_assist_summary` | Call Assist | light |
| `call_assist_template` | Call Assist | light |
| `call_assist_timeline` | Call Assist | light |
| `call_assist_timeline_clear` | Call Assist | light |
| `call_assist_timeline_export` | Call Assist | light |
| `call_assist_timeline_stats` | Call Assist | light |
| `call_assist_timeline_summary` | Call Assist | light |
| `call_assist_timeline_to_history` | Call Assist | light |
| `call_check_auto_end` | Phase 3 | excluded |
| `call_estimate_cost` | Phase 3 | excluded |
| `call_session_add_transcript` | Call Session | light |
| `call_session_create` | Call Session | light |
| `call_session_end` | Call Session | light |
| `call_session_get` | Call Session | light |
| `call_session_list` | Call Session | light |
| `call_session_update_status` | Call Session | light |
| `cancel_scheduled_recording` | Scheduling | light |
| `cancel_transcribe_job` | Transcription | light |
| `cancel_transcription` | Queue | light |
| `check_audio_duplicate` | Audio | light |
| `check_duplicate` | History | light |
| `check_hotwords` | Hotwords | light |
| `check_integrity` | Integrity | heavy |
| `check_migration` | Migration | light |
| `cleanup_old_history` | History | light |
| `clear_search_history` | Search | light |
| `compact_history` | History | light |
| `compare_periods` | Analytics | heavy |
| `compare_recordings` | Text | light |
| `compare_texts` | Text | medium |
| `configure_auto_export` | Auto-export | light |
| `configure_obsidian_sync` | Obsidian | light |
| `convert_audio` | Audio | heavy |
| `create_collection` | Collections | light |
| `create_config_preset` | Settings | light |
| `delete_collection` | Collections | light |
| `detect_emotion` | Text | light |
| `detect_language` | Text | light |
| `detect_voice_activity` | Audio | light |
| `end_chain` | Chains | light |
| `enqueue_transcription` | Queue | light |
| `enrich_recording` | Enrichment | light |
| `expand_abbreviations` | Text | light |
| `export_history` | Export | heavy |
| `export_history_csv` | Export | heavy |
| `export_history_json` | Export | heavy |
| `export_history_markdown` | Export | heavy |
| `export_history_srt` | Export | heavy |
| `export_html_report` | Export | light |
| `export_obsidian` | Export | heavy |
| `export_settings` | Settings | light |
| `export_timeline` | Analytics | light |
| `extract_learning_vocabulary` | Learning | light |
| `extract_terms` | Text | medium |
| `filter_by_confidence` | History | medium |
| `find_duplicates` | History | medium |
| `format_for_paste` | Text | light |
| `fuzzy_search` | Search | medium |
| `generate_auto_title` | Text | light |
| `generate_daily_digest` | Analytics | heavy |
| `generate_flashcards` | Learning | light |
| `generate_html_report` | Export | light |
| `generate_mini_stats_report` | Analytics | light |
| `generate_stats_report` | Analytics | light |
| `get_activity_calendar` | Analytics | light |
| `get_analytics_dashboard` | Analytics | light |
| `get_annotation` | History | light |
| `get_archive_stats` | Archive | light |
| `get_audio_devices` | Recording | light |
| `get_audio_info` | Audio | light |
| `get_auto_backup_status` | Backup | light |
| `get_call_assist_state` | Call Assist | excluded |
| `get_chain` | Chains | light |
| `get_clipboard_history` | History | light |
| `get_collection_items` | Collections | light |
| `get_context_memory` | Text | light |
| `get_daily_cost_summary` | Cost | light |
| `get_dedup_stats` | History | light |
| `get_diagnostics` | Diagnostics | medium |
| `get_error_report` | Diagnostics | medium |
| `get_error_stats` | Diagnostics | medium |
| `get_event_log` | Events | medium |
| `get_event_stats` | Events | medium |
| `get_export_schedule_status` | Auto-export | light |
| `get_favorites` | History | light |
| `get_feature_flags` | Flags | light |
| `get_history_item` | History | light |
| `get_history_overview` | History | medium |
| `get_history_page` | History | light |
| `get_history_stats` | History | medium |
| `get_history_statistics` | History | medium |
| `get_hotwords` | Hotwords | light |
| `get_keyword_cloud` | Analytics | light |
| `get_last_llm_diff` | LLM | light |
| `get_learning_stats` | Learning | light |
| `get_metrics_dashboard` | Diagnostics | medium |
| `get_model_cache_info` | Model Cache | light |
| `get_most_replayed` | Playback | light |
| `get_notification_preferences` | Settings | light |
| `get_obsidian_sync_status` | Obsidian | light |
| `get_playback_stats` | Playback | light |
| `get_plugin_info` | Plugins | light |
| `get_popular_searches` | Search | light |
| `get_queue_status` | Queue | light |
| `get_recording_insights` | Recording | medium |
| `get_recording_state` | Recording | excluded |
| `get_recording_stats` | Recording | medium |
| `get_recent_searches` | Search | light |
| `get_sentiment_trends` | Analytics | light |
| `get_session_history` | Recording | medium |
| `get_session_stats` | Recording | medium |
| `get_settings` | Settings | excluded |
| `get_shared` | Sharing | light |
| `get_shutdown_status` | Diagnostics | light |
| `get_smart_vocabulary_suggestions` | Vocabulary | light |
| `get_speaker_aliases` | Speakers | light |
| `get_speaker_statistics` | Analytics | light |
| `get_startup_diagnostics` | Diagnostics | light |
| `get_storage_info` | History | light |
| `get_system_info` | Diagnostics | medium |
| `get_throttle_stats` | Diagnostics | light |
| `get_timeline_view` | Analytics | light |
| `get_topic_timeline` | Analytics | light |
| `get_transcript_versions` | Versioning | light |
| `get_transcripts_path` | History | light |
| `get_transcribe_progress` | Transcription | light |
| `get_usage_stats` | Analytics | medium |
| `get_wake_word_config` | Wake Word | light |
| `get_waveform` | Audio | heavy |
| `health_check` | Diagnostics | medium |
| `import_history_ndjson` | History | light |
| `import_settings` | Settings | light |
| `is_favorite` | History | light |
| `list_abbreviations` | Text | light |
| `list_all_tags` | Tags | light |
| `list_archived` | Archive | light |
| `list_auto_exports` | Auto-export | light |
| `list_backups` | History | light |
| `list_cached_models` | Model Cache | light |
| `list_call_assist_quick_phrases` | Call Assist | light |
| `list_chains` | Chains | light |
| `list_collections` | Collections | light |
| `list_config_presets` | Settings | light |
| `list_normalization_profiles` | Settings | light |
| `list_paste_formatters` | Text | light |
| `list_plugins` | Plugins | light |
| `list_post_process_steps` | Text | light |
| `list_profile_presets` | Settings | excluded |
| `list_scheduled_recordings` | Scheduling | light |
| `list_shared` | Sharing | light |
| `list_summary_profiles` | LLM | light |
| `list_transcription_queue` | Queue | light |
| `list_webhooks` | Webhooks | light |
| `live_subs_ingest` | Live Subs | excluded |
| `live_subs_stop` | Live Subs | excluded |
| `merge_chain_text` | Chains | light |
| `merge_recordings` | History | light |
| `ping` | Recording | excluded |
| `post_process_text` | Text | light |
| `prepare_share` | Sharing | light |
| `preview_merge` | History | light |
| `preview_transcribe_paths` | Transcription | heavy |
| `profile_noise` | Audio | light |
| `record_playback` | Playback | light |
| `register_webhook` | Webhooks | light |
| `remove_abbreviation` | Text | light |
| `remove_from_collection` | Collections | light |
| `remove_hallucination_pattern` | Hallucination | light |
| `remove_hotword` | Hotwords | light |
| `remove_speaker_alias` | Speakers | light |
| `remove_tag` | Tags | light |
| `remove_template` | Templates | light |
| `repair_integrity` | Integrity | heavy |
| `repaste_item` | History | light |
| `replay_events` | Events | medium |
| `restore_history` | History | light |
| `revert_transcript_version` | Versioning | light |
| `run_deduplication` | History | light |
| `run_migration` | Migration | light |
| `run_obsidian_sync` | Obsidian | light |
| `save_transcript_version` | Versioning | light |
| `schedule_recording` | Scheduling | light |
| `score_readability` | Text | light |
| `score_transcription` | Text | light |
| `search_annotations` | History | medium |
| `search_by_speaker` | Search | medium |
| `search_by_tag` | Search | medium |
| `search_history` | Search | medium |
| `search_with_highlights` | Search | light |
| `select_model` | Vocabulary | light |
| `send_to_telegram` | Telegram | light |
| `set_annotation` | History | light |
| `set_feature_flag` | Flags | light |
| `set_notification_preferences` | Settings | light |
| `set_paste_status` | Recording | excluded |
| `set_settings` | Settings | excluded |
| `set_speaker_alias` | Speakers | light |
| `set_translation_glossary_item` | Translation | light |
| `set_wake_word_config` | Wake Word | light |
| `start_call_assist` | Call Assist | light |
| `start_chain` | Chains | light |
| `start_recording` | Recording | excluded |
| `stop_call_assist` | Call Assist | light |
| `stop_recording` | Recording | excluded |
| `suggest_medical_glossary_terms` | Translation | light |
| `summarize_item` | LLM | heavy |
| `summarize_text` | LLM | heavy |
| `synthesize_speech` | TTS | light |
| `test_microphone` | Recording | light |
| `toggle_favorite` | History | light |
| `transcribe_paths` | Transcription | heavy |
| `transcribe_paths_async` | Transcription | light |
| `translate_selection` | Translation | excluded |
| `translate_text` | Translation | medium |
| `unarchive_items` | Archive | light |
| `unregister_webhook` | Webhooks | light |
| `wake_word_list_models` | Wake Word | light |
| `wake_word_start` | Wake Word | light |
| `wake_word_status` | Wake Word | light |
| `wake_word_stop` | Wake Word | light |
| `word_frequency_analysis` | History | medium |
| `get_glossary_suggestions` | Translation | medium |
| `get_vocabulary_suggestions` | Translation | medium |
| `remove_translation_glossary_item` | Translation | light |
