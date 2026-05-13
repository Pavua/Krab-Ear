# IPC API Reference — Backfill 2026-05

**Date:** 2026-05-12  
**Scope:** 29 high-value methods across 4 categories (Wave 45 drift report: 271 undocumented methods)  
**Drift report:** `docs/drift-report-2026-05-12.md`  
**Base reference:** `docs/IPC_API_REFERENCE.md` (78 methods documented, 241 total dispatch entries as of PR #243)  

This file documents the next tier of high-traffic undocumented methods. Every `## method_name` heading matches the exact dispatch key in `BackendService._dispatch`.

---

## Call Session API (Phase 3)

These methods manage outbound call sessions (state machine: `idle → dialing → connected → talking → ending → completed/failed`). Sessions are persisted via `CallSessionStore` (NDJSON, `call_sessions.ndjson`).

| Method | Description |
|---|---|
| `call_session_add_transcript` | Append a speaker turn to a session's transcript history |
| `call_session_create` | Create a new call session record |
| `call_session_end` | Finalize session — mark completed or failed, compute cost/duration |
| `call_session_get` | Fetch full session record by ID |
| `call_session_list` | List sessions with optional status filter |
| `call_session_update_status` | Drive the session state machine to a new status |
| `start_call_assist` | Start real-time call assist session with Voice Gateway integration |
| `stop_call_assist` | Stop active call assist session, optionally generate summary |

---

### `call_session_add_transcript`

Appends a speaker turn to an existing call session's `transcript_history` list.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Session ID |
| `speaker` | string | yes | `"user"` \| `"bot"` \| `"operator"` |
| `text` | string | yes | Transcript text of this turn |
| `ts` | string | no | ISO 8601 timestamp; defaults to server time |

**Response:** `{ok: true, result: {session_id, transcript_count}}`  
**Error:** `{ok: false, error: {code: "invalid_params", message: "..."}}`

```json
{"id":"r1","method":"call_session_add_transcript","params":{"id":"sess_abc123","speaker":"user","text":"Добрый день, я звоню по поводу заказа."}}
```

---

### `call_session_create`

Creates a new call session record in `IDLE` status.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `phone` | string | yes | Destination phone number (E.164 or free-form) |
| `goal_text` | string | yes | Human-readable call objective for AI context |

**Response:** `{ok: true, result: {session_id, status: "idle", created_at}}`  
**Error:** `{ok: false, error: {code: "invalid_params", message: "phone required"}}`

```json
{"id":"r1","method":"call_session_create","params":{"phone":"+79991234567","goal_text":"Узнать статус заказа №12345"}}
```

---

### `call_session_end`

Finalizes a call session. Computes total `duration_sec` and records `end_reason`. Transitions to `COMPLETED` or `FAILED`.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Session ID |
| `reason` | string | no | `"completed"` \| `"no_answer"` \| `"voicemail"` \| `"opt_out"` \| `"timeout"` (default: `"completed"`) |
| `cost_usd` | float | no | Actual telephony cost in USD (default: `0.0`) |
| `failed` | bool | no | If `true`, transitions to `FAILED` instead of `COMPLETED` |

**Response:** `{ok: true, result: {session_id, status, duration_sec, cost_usd, end_reason}}`

```json
{"id":"r1","method":"call_session_end","params":{"id":"sess_abc123","reason":"completed","cost_usd":0.42}}
```

---

### `call_session_get`

Returns the full `CallSession` record as a dict.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Session ID |

**Response:** `{ok: true, result: {id, phone_number, goal_text, status, created_at, started_at, ended_at, duration_sec, cost_usd, end_reason, transcript_history: [...]}}`  
**Error:** `{ok: false, error: {code: "not_found", message: "Сессия не найдена: ..."}}`

```json
{"id":"r1","method":"call_session_get","params":{"id":"sess_abc123"}}
```

---

### `call_session_list`

Lists call sessions, newest first, with optional status filter.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | int | no | Max results (1–500, default: `50`) |
| `status_filter` | string | no | Filter by status: `"idle"` \| `"dialing"` \| `"connected"` \| `"talking"` \| `"ending"` \| `"completed"` \| `"failed"` |

**Response:** `{ok: true, result: {sessions: [...CallSession], total: N}}`

```json
{"id":"r1","method":"call_session_list","params":{"limit":10,"status_filter":"completed"}}
```

---

### `call_session_update_status`

Drives the session state machine to a new status. The store validates allowed transitions.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Session ID |
| `new_status` | string | yes | Target status (see state machine above) |

**Response:** `{ok: true, result: {session_id, status}}`  
**Error:** `{ok: false, error: {code: "invalid_transition", message: "..."}}`

```json
{"id":"r1","method":"call_session_update_status","params":{"id":"sess_abc123","new_status":"dialing"}}
```

---

### `start_call_assist`

Starts a real-time call assistance session. Begins microphone recording, connects to Voice Gateway (if configured), and starts the assist loop for live STT + translation.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `capture_source_mode` | string | no | `"mic"` \| `"system_audio"` \| `"mic_plus_system"` (default from settings) |
| `translation_mode` | string | no | Translation mode (default from settings, e.g. `"auto_to_ru"`) |
| `tts_mode` | string | no | `"local"` \| `"cloud"` \| `"hybrid"` (default: `"hybrid"`) |
| `notify_mode` | string | no | `"auto_on"` \| `"auto_off"` (default from settings) |
| `auto_summary` | bool | no | Generate session summary on stop (default from settings) |
| `phone` | string | no | Phone number for Sentry breadcrumb (masked in logs) |

**Response:** `{ok: true, result: {active, status, session_id, gateway_session_id, gateway_status, gateway_error, capture_source_mode, translation_mode, notify_mode, tts_mode, auto_summary, started_at}}`

```json
{"id":"r1","method":"start_call_assist","params":{"capture_source_mode":"mic","translation_mode":"ru_to_es"}}
```

---

### `stop_call_assist`

Stops the active call assist session. If `auto_summary` is true (or from settings), sends a summary request to Voice Gateway and saves it as a history item.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `auto_summary` | bool | no | Override settings: whether to generate summary (default from settings) |
| `summary_max_items` | int | no | Max transcript items for summary (1–200, default: `40`) |

**Response:** `{ok: true, result: {active: false, status: "stopped", session_id, stopped_at, summary_status, summary?, ...}}`  
`summary_status`: `"ok"` \| `"skipped"` \| `"error"`

```json
{"id":"r1","method":"stop_call_assist","params":{"auto_summary":true}}
```

---

## Live Subtitles API (Phase 2B)

Streaming STT + translation for system audio live subtitles. Swift `SystemAudioCapture` (ScreenCaptureKit) feeds 16 kHz PCM chunks. The service accumulates ≥3 s then flushes: Whisper STT → translate → emits `live_subs.result` event via EventBus → SSE → `LiveSubtitlesOverlay`.

| Method | Description |
|---|---|
| `live_subs_ingest` | Push a base64-encoded PCM chunk into the live subtitles buffer |
| `live_subs_stop` | Flush remaining buffer and reset the session |

---

### `live_subs_ingest`

Feeds a PCM audio chunk into the accumulation buffer. Auto-flushes when buffer reaches ≥3 s or when `is_final=true`. After flush, emits `live_subs.result` event on the EventBus (picked up by SSE stream for `LiveSubtitlesOverlay`).

Audio must be 16 kHz mono PCM int16. If the source sample rate differs (e.g. 48 kHz from ScreenCaptureKit), the service resamples via `scipy.signal.resample_poly`.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `audio_chunk` | string | yes | Base64-encoded raw PCM int16 bytes |
| `sample_rate` | int | no | Input sample rate in Hz (default: `16000`); service resamples to 16 kHz if needed |
| `target_lang` | string | no | Translation target: `"ru"` \| `"es"` \| `"en"` \| `"off"` (default: `"off"`) |
| `is_final` | bool | no | Force flush even if buffer < 3 s (default: `false`) |

**Response (no flush):** `{ok: true, result: {status: "accepted", buffer_duration_sec: float}}`  
**Response (flushed):** `{ok: true, result: {status: "flushed", buffer_duration_sec: float, text: str|null, translation: str|null}}`  
**Error:** `{ok: false, error: {code: "invalid_params", message: "audio_chunk: invalid base64: ..."}}`

```json
{"id":"r1","method":"live_subs_ingest","params":{"audio_chunk":"AAAA...","sample_rate":16000,"target_lang":"ru","is_final":false}}
```

---

### `live_subs_stop`

Flushes any remaining accumulated audio and resets the session buffer. Call this when the user stops system audio capture. After this call, `live_subs.result` will emit one final event if there was buffered audio.

**Params:** None

**Response:** `{ok: true, result: {status: "stopped", flushed: bool}}`  
`flushed` is `true` if there was buffered audio that was processed.

```json
{"id":"r1","method":"live_subs_stop","params":{}}
```

---

## Error Bus / Loud Errors (Phase B)

Phase B "Loud Errors" infrastructure. `ErrorBus` maintains a 200-item ring buffer of `KrabError` objects. Swift can subscribe to these via SSE `error.*` events. 19 error codes defined in `backend/error_codes.py`.

| Method | Description |
|---|---|
| `clear_recent_errors` | Clear the ring buffer and dedupe state |
| `handle_error_action` | Execute an actionable recovery step from a toast button |
| `list_recent_errors` | Retrieve last N errors from the ring buffer |
| `probe_llm_http` | One-shot health ping to LM Studio HTTP endpoint |
| `report_hotkey_conflict` | Swift → backend: global hotkey RegisterEventHotKey failed |
| `report_paste_failure` | Swift → backend: accessibility paste failed |
| `report_reconnect` | Swift → backend: IPC reconnect telemetry |

---

### `clear_recent_errors`

Clears the `ErrorBus` ring buffer and resets dedupe state. Used by diagnostics panel "Clear Errors" button.

**Params:** None

**Response:** `{ok: true, result: {cleared: N}}`  
`N` is the number of entries that were removed.

```json
{"id":"r1","method":"clear_recent_errors","params":{}}
```

---

### `handle_error_action`

Executes a recovery action identified by `action_id` from `ERROR_REGISTRY`. Called when the user taps an action button in `ErrorToastView`. Dispatches via `ACTION_HANDLERS` in `backend/error_actions.py`.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `action_id` | string | yes | Action identifier from `KrabError.action_id`, e.g. `"open_privacy_settings"`, `"disable_rewriter"` |

**Response:** `{ok: true, result: {executed: bool, reason: str|null, side_effect: any|null}}`  
**Error (missing action_id):** `{ok: true, result: {executed: false, reason: "missing action_id", side_effect: null}}`

```json
{"id":"r1","method":"handle_error_action","params":{"action_id":"open_privacy_settings"}}
```

---

### `list_recent_errors`

Returns the last N `KrabError` entries from the in-memory ring buffer (max 200 items). Used by the Diagnostics tab and Swift `ErrorActionHandler`.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `limit` | int | no | Max entries to return (default: `200`) |

**Response:** `{ok: true, result: {errors: [KrabError, ...]}}`

Each `KrabError` object:
```json
{
  "severity": "warn",
  "component": "paste",
  "code": "paste.ax_denied",
  "message_user": "Нет доступа к Accessibility...",
  "message_debug": "paste failed reason=ax_denied app=com.apple.Notes",
  "timestamp": "2026-05-12T10:00:00Z",
  "context": {"app_bundle": "com.apple.Notes", "reason": "ax_denied"},
  "actionable": true,
  "action_id": "open_privacy_settings"
}
```

```json
{"id":"r1","method":"list_recent_errors","params":{"limit":50}}
```

---

### `probe_llm_http`

Fires a single HTTP health check against the LM Studio endpoint (GET `/v1/models`). Returns reachability and latency. Used by `LLMHttpProbe` and the Diagnostics panel.

**Params:** None

**Response:** `{ok: true, result: {reachable: bool, latency_ms: int, model: str|null}}`  
`latency_ms` is `0` if the rewriter is not initialized.  
`model` is the configured model name (e.g. `"gemma-4-e4b-it-mlx"`), or `null`.

```json
{"id":"r1","method":"probe_llm_http","params":{}}
```

---

### `report_hotkey_conflict`

Swift → backend telemetry: `RegisterEventHotKey` returned `eventHotKeyExistsErr`. Creates a `KrabError` with code `hotkey.conflict` and pushes to `ErrorBus`, which emits an SSE event for toast display.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `chord` | string | no | Chord identifier, e.g. `"right_option"` |

**Response:** `{ok: true, result: {ok: true}}`

```json
{"id":"r1","method":"report_hotkey_conflict","params":{"chord":"right_option"}}
```

---

### `report_paste_failure`

Swift → backend telemetry: accessibility paste failed. Creates a `KrabError` and pushes to `ErrorBus`. Backend maps reason → error code from `ERROR_REGISTRY`.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `reason` | string | yes | `"ax_denied"` — Accessibility permission denied; `"app_unsupported"` — target app does not support AX paste |
| `app_bundle` | string | no | Bundle ID of the target app (e.g. `"com.apple.Notes"`) |

**Response:** `{ok: true, result: {ok: true, code: "paste.ax_denied"}}`  
**Error:** `{ok: true, result: {ok: false, reason: "unknown_paste_reason"}}`

```json
{"id":"r1","method":"report_paste_failure","params":{"reason":"ax_denied","app_bundle":"com.apple.Slack"}}
```

---

### `report_reconnect`

Swift → backend reconnect telemetry. Called after `IPCClient` successfully reconnects after N retries. Pushes an `ipc.reconnect` info-severity event so the user gets visibility on transient IPC breaks via toast.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `attempts` | int | no | Number of retry attempts before success |
| `duration_ms` | int | no | Total elapsed reconnect time in milliseconds |

**Response:** `{ok: true, result: {ok: true}}`

```json
{"id":"r1","method":"report_reconnect","params":{"attempts":3,"duration_ms":1850}}
```

---

## Wake Word + Live Translation (Phase 2A / Voice Assistant)

Wake word detection via `OpenWakeWordAdapter` (Apache-2.0, no signup). Selection translation for Cmd+Shift+T global hotkey (Phase 2A).

| Method | Description |
|---|---|
| `get_wake_word_config` | Read wake word + conversation engine configuration |
| `set_wake_word_config` | Update wake word and conversation engine settings |
| `translate_selection` | Translate selected text for the selection-translate workflow |
| `wake_word_list_models` | List available built-in and custom wake word models |
| `wake_word_start` | Start wake word listener with a specific model |
| `wake_word_status` | Get current listener state (running / active model) |
| `wake_word_stop` | Stop the active wake word listener |

---

### `get_wake_word_config`

Returns the full wake word configuration including availability of required files. Use this to populate the Wake Word settings panel.

**Params:** None

**Response:**
```json
{
  "ok": true,
  "result": {
    "wake_word_enabled": false,
    "access_key_present": false,
    "ppn_present": false,
    "ppn_path": null,
    "engine_preference": "auto",
    "brain_preference": "auto"
  }
}
```

`access_key_present` checks env var `KRAB_EAR_PORCUPINE_ACCESS_KEY` + `<DATA_DIR>/porcupine_access_key` file.  
`ppn_present` checks standard `.ppn` locations under `~/Library/Application Support/KrabEar/`.

```json
{"id":"r1","method":"get_wake_word_config","params":{}}
```

---

### `set_wake_word_config`

Updates wake word and Voice Assistant engine/brain preferences via the settings service.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `wake_word_enabled` | bool | no | Enable or disable wake word detection |
| `conversation_engine` | string | no | `"auto"` \| `"moshi"` \| `"seamless"` |
| `conversation_brain` | string | no | `"auto"` \| `"qwen3-30b"` \| `"qwen3-4b"` |

**Response:** `{ok: true, result: {updated: N, fields: ["wake_word_enabled", ...]}}`  
`updated` is `0` if no valid fields were provided.

```json
{"id":"r1","method":"set_wake_word_config","params":{"wake_word_enabled":true,"conversation_engine":"moshi"}}
```

---

### `translate_selection`

Translates a block of selected text (Phase 2A: Cmd+Shift+T workflow). Auto-detects source language via `LanguageDetector` if not specified. Respects `privacy_mode_enabled` — forces offline-only when active.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes | Selected text to translate (empty string returns immediately with no error) |
| `source_lang` | string | no | ISO 639-1 source language (`"ru"`, `"es"`, `"en"`); auto-detected if omitted |
| `target_lang` | string | no | ISO 639-1 target language; if omitted, uses default mapping: `ru→es`, `es→ru`, `en→ru` |

**Response:**
```json
{
  "ok": true,
  "result": {
    "translated_text": "Hola, ¿cómo estás?",
    "source_lang_detected": "ru",
    "target_lang": "es",
    "engine": "offline_ru_es",
    "latency_ms": 45
  }
}
```

```json
{"id":"r1","method":"translate_selection","params":{"text":"Привет, как дела?"}}
```

---

### `wake_word_list_models`

Lists all available wake word models: built-in openWakeWord models and user-installed custom `.onnx`/`.tflite` models from `<DATA_DIR>/wake_word_models/`.

**Params:** None

**Response:**
```json
{
  "ok": true,
  "result": {
    "ok": true,
    "models": [
      {"name": "hey_jarvis", "type": "builtin"},
      {"name": "Краб_ru_mac_v3_0_0", "type": "custom", "path": "/path/to/Краб_ru_mac_v3_0_0.onnx"}
    ],
    "engine_available": true,
    "custom_models_dir": "/Users/.../KrabEar/wake_word_models"
  }
}
```

`engine_available` is `false` when `openwakeword` package is not installed.

```json
{"id":"r1","method":"wake_word_list_models","params":{}}
```

---

### `wake_word_start`

Starts the wake word listener in a background thread. Logs detection events to the backend logger (level INFO). The listener fires an internal callback on detection, which can trigger recording start.

**Params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `model` | string | no | Model name from `wake_word_list_models` (default: `"hey_jarvis"`) |
| `threshold` | float | no | Detection confidence threshold 0.0–1.0 (default: `0.5`) |

**Response (success):** `{ok: true, result: {ok: true, model: "hey_jarvis", threshold: 0.5}}`  
**Response (error):** `{ok: true, result: {ok: false, error: "openwakeword не установлен"}}`

```json
{"id":"r1","method":"wake_word_start","params":{"model":"hey_jarvis","threshold":0.6}}
```

---

### `wake_word_status`

Returns current listener state without side effects.

**Params:** None

**Response:**
```json
{
  "ok": true,
  "result": {
    "ok": true,
    "running": true,
    "active_model": "hey_jarvis",
    "engine_available": true
  }
}
```

`active_model` is `null` when `running` is `false`.

```json
{"id":"r1","method":"wake_word_status","params":{}}
```

---

### `wake_word_stop`

Stops the active wake word listener. No-op if not running.

**Params:** None

**Response:** `{ok: true, result: {ok: true}}`

```json
{"id":"r1","method":"wake_word_stop","params":{}}
```

---

## Notes on Undocumented Methods

The following methods from the task scope were **not found** in the dispatch table and are likely handled by delegated services under different names:

- `live_translate_*` — no methods with this prefix found in `service.py` dispatch. Live translation for subtitles uses `live_subs_ingest` with a non-`"off"` `target_lang`.
- `live_subs_push_chunk` — alias not found; the actual IPC key is `live_subs_ingest`.

The `handshake` method (Phase B/C) is documented in `docs/IPC_API_REFERENCE.md` Phase B/C section and is omitted here to avoid duplication.
