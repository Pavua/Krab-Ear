# Voice Assistant Mode — Implementation Plan (Phase 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable full-duplex voice conversations with AI in Krab Ear at 160–500 ms latency, supporting Russian (primary), English, and Spanish.

**Architecture:** Three-tier system — Krab Ear UI (tier 1) → Voice Gateway orchestration (tier 2) → Krab agent brain + LLM (tier 3). Lazy-loaded conversation engines (Moshi for EN, SeamlessM4T for RU/ES) stream audio bidirectionally via WebSocket. LLM brain (qwen3-30b-a3b-2507) via Krab agent proxy. Lazy-loading + LRU eviction keeps idle RAM at ~100 MB.

**Tech Stack:** Voice Gateway: Python 3.12, FastAPI, `moshi-mlx`, `seamless-streaming`, `transformers`, `torch+MPS`. Krab Ear: Swift (URLSessionWebSocketTask), AVAudioEngine, Silero wakeword. Krab agent: OpenClaw proxy, Qwen3 LM Studio, MCP tools. All repos use pytest/unittest for tests.

---

## PR 1.1: Voice Gateway — Moshi Engine + LazyConversationEngine Base + WS Handler

**Effort:** M (3-4 days) | **Unblocks:** PR 1.2, 1.3, 1.4, 1.5

Create foundational engine abstraction and Moshi (English) support. Write WebSocket protocol handler. No Krab agent brain integration yet.

### Files to create

```
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/__init__.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/base.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/moshi_engine.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/session_state.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/ws_handler.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/events.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/exceptions.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/tests/conversation/__init__.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/tests/conversation/test_base.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/tests/conversation/test_moshi_engine.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/tests/conversation/test_ws_handler.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/tests/conversation/test_session_state.py
```

### Files to modify

```
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/main.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/config.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/requirements.txt
```

### Tasks

- [ ] **Test-first: LazyConversationEngine ABC lifecycle** (test_base.py)
  - Write failing test: `test_load_async_returns_when_ready`, `test_unload_frees_memory`, `test_last_used_at_timestamp`.
  - Implement `LazyConversationEngine` ABC in base.py with abstract methods `load()`, `unload()`, properties `is_loaded`, `last_used_at`, `language`.
  - Implement `EngineState` dataclass (loaded, language, last_used_at, model_name).
  - Run test, verify PASS.

- [ ] **Test-first: MoshiEngine wrapping moshi-mlx** (test_moshi_engine.py)
  - Write failing test: `test_moshi_load_with_progress`, `test_moshi_process_audio_chunk_full_duplex`, `test_moshi_unload_clears_weights`.
  - Create mock `FakeMoshi` that simulates chunked I/O without actual model.
  - Implement `MoshiEngine(LazyConversationEngine)` in moshi_engine.py:
    - `__init__(model_id="kyutai/moshiko-mlx-q4")` — store model ID.
    - `load(progress_callback)` — async, import moshi-mlx, load model weights, call progress at 25%, 50%, 75%, 100%.
    - `process_audio_chunk(pcm_bytes: bytes) -> (transcript_text: str, output_audio: bytes)` — bidirectional streaming via Moshi API.
    - `unload()` — delete model references, clear KV cache.
    - Property `language = "en"`.
  - Run test, verify PASS.

- [ ] **Test-first: WebSocket protocol** (test_ws_handler.py)
  - Write failing test: `test_ws_uplink_binary_opus_frames`, `test_ws_downlink_stt_partial_event`, `test_ws_control_interrupt`, `test_session_cleanup_on_disconnect`.
  - Implement `ConversationWSHandler` in ws_handler.py:
    - Accept WebSocket upgrade on path `/v1/sessions/{session_id}/conversation`.
    - Uplink: receive binary Opus PCM frames 16 kHz, 80 ms windows; text JSON `{"type": "control", "action": "..."}`.
    - Downlink: send binary Opus 24 kHz frames; text JSON events `{"type": "stt.partial", "text": "...", "lang": "en"}`, `{"type": "engine.loaded", "name": "moshi"}`.
    - Control actions: `interrupt`, `end`, `push_to_talk_off`.
  - Use mock `FakeMoshi` for testing (no real weights).
  - Run test, verify PASS.

- [ ] **Test-first: session state & cleanup** (test_session_state.py)
  - Write failing test: `test_session_init`, `test_session_end_callback`, `test_session_timeout`.
  - Implement `ConversationSessionState` in session_state.py:
    - Fields: `session_id`, `engine`, `start_time`, `is_active`, `transcript_buffer`, `end_callback`.
    - Methods: `mark_ended()`, `cleanup()`, `total_duration()`.
  - Run test, verify PASS.

- [ ] **Implement event protocol** (events.py)
  - Create `ConversationEvent` Pydantic model with EVENT_CONTRACT_V1 envelope: `{type: str, ts: float, data: dict}`.
  - Event types: `stt.partial`, `engine.loaded`, `engine.unload`, `tool.invoked`, `summary.ready`, `error`.
  - Decode/encode JSON frames.

- [ ] **Implement exception taxonomy** (exceptions.py)
  - `ConversationError` base class.
  - Subclasses: `EngineLoadError`, `AudioStreamError`, `SessionNotFoundError`.

- [ ] **Mount WS endpoint in FastAPI** (app/main.py)
  - Add route: `@app.websocket("/v1/sessions/{session_id}/conversation")`.
  - Instantiate handler, connect WebSocket, await handler loop.

- [ ] **Add config vars** (app/config.py)
  - `KRAB_VG_MOSHI_MODEL` (default `"kyutai/moshiko-mlx-q4"`).
  - `KRAB_VG_LRU_TTL_SEC` (default `300`, unload after 5 min idle).

- [ ] **Add dependencies** (requirements.txt)
  - `moshi-mlx>=0.3.0`, `transformers>=4.45`, `torch>=2.5`, `sounddevice>=0.5`, `pydantic`.

---

## PR 1.2: Voice Gateway — SeamlessM4T Engine + Language Routing

**Effort:** M (2-3 days) | **Depends on:** PR 1.1 | **Unblocks:** PR 1.6

Add SeamlessM4T (RU/ES) engine. Implement language detection at session start. Route sessions to correct engine. Lazy-load only one engine at a time.

### Files to create

```
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/seamless_engine.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/language_detect.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/tests/conversation/test_seamless_engine.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/tests/conversation/test_language_detect.py
```

### Files to modify

```
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/ws_handler.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/config.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/requirements.txt
```

### Tasks

- [ ] **Test-first: language detection** (test_language_detect.py)
  - Write failing test: `test_detect_ru_from_audio`, `test_detect_en_from_audio`, `test_detect_es_from_audio`.
  - Implement `detect_language(audio_bytes: bytes) -> str` in language_detect.py:
    - Use Silero language detection model or simple heuristic (phoneme scoring).
    - Return ISO-639-1 code: "en", "ru", "es".
  - Run test with mock audio clips, verify PASS.

- [ ] **Test-first: SeamlessM4TEngine** (test_seamless_engine.py)
  - Write failing test: `test_seamless_load_large_fp16_mps`, `test_seamless_process_multilingual`, `test_seamless_code_switching`.
  - Implement `SeamlessM4TEngine(LazyConversationEngine)` in seamless_engine.py:
    - Load `facebook/seamless-streaming` 2.5B model from HF.
    - `load(progress_callback)` — async, use `torch.float16`, `model.to("mps")`.
    - `process_audio_chunk(pcm_bytes: bytes, language: str)` — streaming S2ST via EMMA agent.
    - `unload()` — release model from MPS memory.
    - Property `language` (set per-session from language detect).
  - Note: Use `model.train(False)` for inference mode (NOT `.eval()` — security hook).
  - Use mock `FakeSeamless` for testing.
  - Run test, verify PASS.

- [ ] **Implement engine routing logic** (ws_handler.py)
  - Add `_active_engine` singleton to handler state.
  - On new session:
    - Detect language from first 1.5 s audio.
    - If language matches `_active_engine.language` → reuse.
    - Else → `_active_engine.unload()`, load new engine (15 s warmup).
  - Track `_active_engine.last_used_at`.
  - Background task: every 10 s, if `time.time() - _active_engine.last_used_at > 300` → `_active_engine.unload()`.

- [ ] **Add config vars** (app/config.py)
  - `KRAB_VG_SEAMLESS_MODEL` (default `"facebook/seamless-streaming"`).

- [ ] **Add dependencies** (requirements.txt)
  - `fairseq2>=0.5`, `pytorch>=2.5`, `sentencepiece`.

---

## PR 1.3: Krab Ear — ConversationViewController + WS Client + UI

**Effort:** M (3-4 days) | **Depends on:** PR 1.1 | **Unblocks:** PR 1.5

Add new "Разговор с AI" tab to Krab Ear `.app`. Implement WebSocket client, audio capture/playback, waveform + transcript display. Integrate into main panel.

### Files to create

```
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+UI.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+WebSocket.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+Audio.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/ConversationEvents.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Tests/KrabEarAgentTests/ConversationViewControllerTests.swift
```

### Files to modify

```
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/Models.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/backend/service.py
```

### Tasks

- [ ] **Test-first: ConversationViewController lifecycle** (ConversationViewControllerTests.swift)
  - Write failing test: `test_viewDidLoad_creates_ui_elements`, `test_connect_websocket_success`, `test_disconnect_cleanup`.
  - Implement `ConversationViewController: NSViewController` in ConversationViewController.swift:
    - `viewDidLoad()` → initialize AVAudioEngine, create WS client, layout UI.
    - `startConversation(sessionId: String)` → open WS to `ws://127.0.0.1:8090/v1/sessions/{sessionId}/conversation`.
    - `endConversation()` → close WS, save transcript, cleanup audio engine.
  - Run test, verify PASS.

- [ ] **Implement UI layout** (ConversationViewController+UI.swift)
  - Create subviews:
    - `waveformView: NSView` — live input + output waveform overlay (2 colors).
    - `transcriptView: NSTextView` — scrollable transcript, updated per `stt.partial` event.
    - `statusLabel: NSTextField` — "🟢 Слушает" / "🟡 Думает" / "🔴 Говорит".
    - `interruptButton: NSButton` — "Прервать AI" (hotkey: Esc).
    - `endButton: NSButton` — "Завершить".
  - Layout via Auto Layout.
  - Update `statusLabel` based on event type: `stt.partial` → "Слушает", `tool.invoked` → "Думает", audio playback → "Говорит".

- [ ] **Implement WebSocket client** (ConversationViewController+WebSocket.swift)
  - Use `URLSessionWebSocketTask`.
  - `connectWebSocket(url: URL)` — async.
  - Send: binary Opus frames (80 ms), text JSON control messages.
  - Receive: binary frames (audio output), text JSON events.
  - Decode events into `ConversationEvent` objects.
  - Route events: `stt.partial` → update transcript, `engine.loaded` → update status, etc.

- [ ] **Implement audio I/O** (ConversationViewController+Audio.swift)
  - Use `AVAudioEngine`, `AVAudioSession` (category: `.playAndRecord`).
  - Capture: PCM 16 kHz mono, encode to Opus 80 ms frames, send uplink.
  - Playback: decode Opus frames, feed to AVAudioEngine output.
  - Waveform visualization: downsample received frames, update `waveformView`.

- [ ] **Implement event decoding** (ConversationEvents.swift)
  - Pydantic-like Codable struct `ConversationEvent`:
    - `type: String` (stt.partial, engine.loaded, tool.invoked, summary.ready, error).
    - `data: [String: Any]`.
  - Helper: `decode(json: String) -> ConversationEvent?`.

- [ ] **Add tab to HistoryPanelController** (HistoryPanelController.swift)
  - Add 4th tab segment "Разговор с AI" (index 3).
  - Tab callback: instantiate `ConversationViewController`, add to tab view.

- [ ] **Add IPC method** (KrabEar/backend/service.py)
  - `get_voice_config()` → returns dict: `{voice_gateway_url, language_hint, engine_preference, brain_preference}`.
  - Used by Swift UI to configure initial settings.

- [ ] **Write integration test** (ConversationViewControllerTests.swift)
  - Mock WebSocket server at `127.0.0.1:9999`.
  - Connect `ConversationViewController` to mock.
  - Send mock events, verify UI updates.

---

## PR 1.4: Krab Agent — voice_channel_handler + Brain Proxy to OpenClaw

**Effort:** M (2-3 days) | **Depends on:** PR 1.1, 1.3

Add voice channel coordinator to Krab agent. Implement brain proxy (HTTP client to Krab agent's OpenClaw router). Add MCP tools for voice assistant use cases.

### Files to create

```
/Users/pablito/Antigravity_AGENTS/Краб/src/voice_channel/__init__.py
/Users/pablito/Antigravity_AGENTS/Краб/src/voice_channel/voice_channel_handler.py
/Users/pablito/Antigravity_AGENTS/Краб/src/voice_channel/voice_routes.py
/Users/pablito/Antigravity_AGENTS/Краб/src/voice_channel/voice_state.py
/Users/pablito/Antigravity_AGENTS/Краб/src/mcp_tools/voice_assistant_tools.py
/Users/pablito/Antigravity_AGENTS/Краб/tests/voice_channel/__init__.py
/Users/pablito/Antigravity_AGENTS/Краб/tests/voice_channel/test_voice_channel_handler.py
```

### Files to modify

```
/Users/pablito/Antigravity_AGENTS/Краб/src/openclaw_client.py
/Users/pablito/Antigravity_AGENTS/Краб/src/model_manager.py
/Users/pablito/Antigravity_AGENTS/Краб/src/mcp_client.py
```

### Tasks

- [ ] **Test-first: voice_channel_handler** (test_voice_channel_handler.py)
  - Write failing test: `test_process_stt_text`, `test_invoke_brain_via_openClaw`, `test_stream_response_back`.
  - Implement `VoiceChannelHandler` in voice_channel_handler.py:
    - Constructor: takes `openclaw_client`, `memory_engine`, `mcp_client`.
    - `process_transcript(text: str, language: str) -> AsyncIterator[str]`:
      - Build system prompt (reuse existing RU-tuned Krab prompts, adapt to language).
      - Call `openclaw_client.chat_completion(messages, model_hint="voice_assistant", stream=True)`.
      - Yield response tokens as they arrive.
    - `on_conversation_end(transcript: str, summary: str, language: str)` → save to memory + Krab Ear history.
  - Run test, verify PASS.

- [ ] **Implement FastAPI routes** (voice_routes.py)
  - `POST /v1/voice/message` — accept `{text: str, language: str, session_id: str}` → returns streaming response.
  - Use handler to process transcript, return LLM response stream.

- [ ] **Implement voice state** (voice_state.py)
  - `VoiceSessionState: BaseModel` — `session_id`, `language`, `transcript_buffer`, `start_time`.
  - Simple dict-based state store (no persistence; ephemeral per conversation).

- [ ] **Implement MCP tools** (voice_assistant_tools.py)
  - `voice_assistant_get_recent_dictations(n: int)` — read Krab Ear history NDJSON.
  - `voice_assistant_send_telegram(chat_id: str, text: str)` → call Krab userbot bridge.
  - `voice_assistant_search_memory(query: str)` → search persistent memory.
  - `voice_assistant_get_weather(location: str)` → external tool stub (deferred implementation).
  - Tools registered in MCP client.

- [ ] **Register tools in MCP client** (src/mcp_client.py)
  - Import `voice_assistant_tools`, add to tool registry.

- [ ] **Hook model routing** (src/model_manager.py)
  - If `model_hint == "voice_assistant"` → prefer fast model (qwen3-4b for fallback, qwen3-30b for primary).

- [ ] **Hook OpenClaw registry** (src/openclaw_client.py)
  - Ensure voice tools callable from OpenClaw brain.

---

## PR 1.5: Triggers — GUI Button + Right Option Double-Tap + Silero Wake Word

**Effort:** S (1-2 days) | **Depends on:** PR 1.3

Add three conversation triggers. Integrate into existing HotkeyManager. Add Silero wake word listener.

### Files to create

```
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/WakeWordListener.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/HotkeyDoubleTapDetector.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Tests/KrabEarAgentTests/WakeWordListenerTests.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Resources/silero_wakeword_kr.mlmodelc/
```

### Files to modify

```
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/HotkeyManager.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/Models.swift
```

### Tasks

- [ ] **Implement wake word listener** (WakeWordListener.swift)
  - Load Silero CoreML model (silero_wakeword_kr.mlmodelc) at app launch.
  - Maintain rolling 5 s audio buffer.
  - Run inference every 100 ms → detect "Краб" trigger phrase.
  - If detected → call callback `onWakeWordDetected()`.
  - Toggle in Settings: "Wake Word Detection" (off by default for privacy).

- [ ] **Implement hotkey double-tap detector** (HotkeyDoubleTapDetector.swift)
  - Monitor Right Option key presses via `NSEvent.addLocalMonitorForEvents(matching: .flagsChanged)`.
  - Detect two taps within 300 ms.
  - On double-tap → toggle conversation (start if stopped, stop if running).

- [ ] **Add GUI button to ConversationViewController+UI** (ConversationViewController+UI.swift)
  - Big "🎙 Начать разговор" button in conversation tab.
  - On click → call `startConversation()`.

- [ ] **Hook hotkey detector into HotkeyManager** (HotkeyManager.swift)
  - Instantiate `HotkeyDoubleTapDetector`, wire callback to ConversationViewController.
  - Distinguish from existing Right Option single-hold (dictation).

- [ ] **Add Settings panel controls** (HistoryPanelController+Settings.swift)
  - "Разговор с AI" section in Settings tab:
    - Toggle "Включить горячую клавишу" (default: on).
    - Toggle "Детектор пробуждения 'Краб'" (default: off).
    - Dropdown: "Предпочтительный движок" (auto, moshi, seamless).
    - Dropdown: "Мозг LLM" (qwen3-30b, qwen3-4b, openclaw).

- [ ] **Add ConversationConfig struct** (Models.swift)
  - Fields: `hotkeyEnabled`, `wakeWordEnabled`, `enginePreference`, `brainPreference`.
  - Store in UserDefaults.

---

## PR 1.6: qwen3-30b Setup in LM Studio + Auto-Eviction Policy

**Effort:** XS (0.5 day) | **Depends on:** PR 1.4

Configure qwen3-30b-a3b-2507 in LM Studio. Implement auto-eviction logic in Voice Gateway (unload 4b if 30b loads, etc.).

### Files to modify

```
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/brain_proxy.py
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/config.py
/Users/pablito/Antigravity_AGENTS/Краб/src/model_manager.py
```

### Tasks

- [ ] **Document LM Studio config** (README or inline comment)
  - Use `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit` (17.2 GB).
  - Settings: context_length 8192, gpu_offload max, kv_cache_quantization q8_0, max_tokens 512, temperature 0.7, top_p 0.8.
  - Port: 1234 (OpenAI-compatible `/v1/chat/completions`).

- [ ] **Implement brain proxy** (brain_proxy.py)
  - `AsyncBrainProxy` class:
    - HTTP client to LM Studio at `http://127.0.0.1:1234`.
    - `chat_completion(messages, temperature, max_tokens, stream=False)` → calls LM Studio endpoint.
    - Streaming: yield tokens as SSE chunks.

- [ ] **Add auto-eviction hints** (app/config.py)
  - `KRAB_LM_STUDIO_URL` (default `http://127.0.0.1:1234`).
  - Add note: "If both models loaded in LM Studio, manual unload required or restart LM Studio."

- [ ] **Model routing hint** (src/model_manager.py)
  - Suggestion: check available models, escalate to 30b if available and context large (>200 tok).

---

## PR 1.7: (Optional) XTTS-v2 Voice Clone Fallback

**Effort:** M (2 days) | **Depends on:** PR 1.2

If SeamlessM4T audio quality insufficient, fallback to XTTS-v2 for TTS (optional per acceptance criteria; deferred if not blocking).

### Files to create

```
/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/app/conversation/xtts_engine.py
```

### Tasks

- [ ] **Implement XTTS-v2 TTS** (xtts_engine.py)
  - Load `coqui/XTTS-v2` model.
  - `synthesize(text: str, language: str) -> bytes` → returns Opus-encoded audio.
  - Cache speaker embeddings to speed up inference.

---

## PR 1.8: E2E Acceptance Tests + Integration Tests

**Effort:** M (3 days) | **Depends on:** All above

Write comprehensive acceptance tests covering RU/EN/ES, code-switching, privacy mode, session recycling, history save.

### Files to create

```
/Users/pablito/Antigravity_AGENTS/Krab Ear/tests/integration/test_voice_assistant_e2e.py
```

### Tasks

- [ ] **Test: RU conversation end-to-end** (test_voice_assistant_e2e.py)
  - User triggers → conversation starts → detect Russian → load SeamlessM4T → capture audio (1.5 s Russian sentence) → transcribe → send to Krab agent → LLM replies in Russian → audio output plays → transcript + summary saved to history.
  - Assert: `"Разговор с AI"` entry in Krab Ear history NDJSON.
  - Assert: latency p50 ≤ 2.5 s (research-realistic for SeamlessStreaming + qwen3-30b TTFT).

- [ ] **Test: EN conversation (Moshi)** (test_voice_assistant_e2e.py)
  - English audio → auto-route to Moshi → 160–300 ms latency target (research: 200 ms Moshi + WS bridge).
  - Assert: latency p50 ≤ 300 ms.

- [ ] **Test: ES conversation** (test_voice_assistant_e2e.py)
  - Spanish audio → SeamlessM4T → Spanish LLM reply.
  - Assert: accuracy ≥ 90% on test corpus (5 sentences read aloud).

- [ ] **Test: code-switching RU↔EN mid-sentence** (test_voice_assistant_e2e.py)
  - Audio: "Привет, can you help me with that?" → transcribe → LLM processes → reply includes both languages.
  - Assert: accuracy ≥ 80%, no crash.

- [ ] **Test: mid-response interruption** (test_voice_assistant_e2e.py)
  - AI speaking → user speaks → AI pauses within 200 ms.
  - Assert: interrupt button works, transcript updates.

- [ ] **Test: privacy mode** (test_voice_assistant_e2e.py)
  - Enable "Приватный режим" toggle → conversation runs → history NDJSON does NOT contain entry OR entry marked `private=true`.
  - Assert: no transcript persisted.

- [ ] **Test: Moshi 4-min session recycler** (test_voice_assistant_e2e.py)
  - Start 5-min conversation with Moshi → auto-restart at 4 min (criterion 8c).
  - User sees seamless transition (status changes briefly to "Переподключение" then back to "Слушает").
  - Assert: no kernel panic, conversation continues.

- [ ] **Test: hardware budget** (test_voice_assistant_e2e.py)
  - Start active RU conversation (SeamlessM4T + qwen3-30b) → measure RAM.
  - Assert: total ≤ 33 GB (research: 10 GB Seamless + 17 GB qwen3-30b + 5 GB OS + 1 GB buffer).

---

## Implementation Order

1. **PR 1.1** (VG: Moshi + base): Foundational. Blocks 1.2, 1.3, 1.4.
2. **PR 1.2** (VG: Seamless) and **PR 1.3** (Ear: UI) in parallel after 1.1.
3. **PR 1.4** (Krab agent: handler) after 1.1 & 1.3 (needs WS contract, controller to exist).
4. **PR 1.5** (Triggers) after 1.3 (controller must exist).
5. **PR 1.6** (LM Studio) after 1.4 (brain proxy ready).
6. **PR 1.7** (XTTS, optional) after 1.2 (if needed).
7. **PR 1.8** (E2E) after all.

---

## Acceptance Criteria (from spec section 11)

All criteria must PASS before Phase 1 considered done:

1. Open Krab Ear `.app` → click "Разговор с AI" tab → click "🎙 Начать разговор" → speak Russian → AI replies in Russian within 1 s.
2. Right Option double-tap from anywhere → conversation starts; double-tap again → ends.
3. Wake word "Краб" detected → conversation starts (toggle on in Settings).
4. Mid-AI-response, user starts speaking → AI stops mid-sentence within 200 ms (full-duplex).
5. Conversation transcript + summary saved to history (visible in Krab Ear "История" tab) and to Krab agent memory (queryable from Telegram).
6. RU conversation accuracy ≥ 95% on test corpus (10 sentences read aloud).
7. EN conversation latency p50 ≤ 300 ms (Moshi + WS bridge overhead).
8. RU conversation first-audio latency p50 ≤ 2.5 s (SeamlessStreaming 1–2 s + qwen3-30b TTFT 0.5 s).
8a. ES conversation accuracy ≥ 90% on test corpus; latency p50 ≤ 2.5 s.
8b. Code-switching test: 5-sentence mixed RU↔EN conversation completes without crash, accuracy ≥ 80%.
8c. Moshi long-session recycler: auto-restart conversation after 4 min (avoid 5-min buffer kernel panic). User sees seamless transition.
9. Hardware: total RAM during active RU conversation ≤ 33 GB.
10. Privacy mode toggle works — no transcript persisted.

---

## Known Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|-----------|
| SeamlessM4T RU audio quality below 95% | Medium | Fallback to XTTS-v2 RU voice (PR 1.7) |
| qwen3-30b too slow on M4 Max (>2s response) | Low-Medium | Auto-fallback to qwen3-4b for short queries |
| MLX version conflict (moshi-mlx pins mlx<0.18) | High | Pin both to compatible range; test early in PR 1.1 |
| Moshi 5-min buffer kernel panic on long sessions | High | Auto-recycler in PR 1.1 design (4-min restart) |
| Voice Gateway single-point-of-failure | Low (dev) | Krab Ear local fallback (Phase C deferred) |
| PyTorch MPS regression on macOS 26 | Medium | Test on current macOS; CPU mode fallback if broken |

---

## Research Citations

All research conducted 2026-04-17:

- **Moshi:** `/tmp/krab-ear-research/moshi_mlx_state.md` — `kyutai/moshiko-mlx-q4` 160–200 ms, 8–12 GB RAM, CC-BY-4.0, **5-min buffer cap**, no WS server (write own bridge).
- **SeamlessStreaming:** `/tmp/krab-ear-research/seamless_mlx_state.md` — **No MLX port**, PyTorch+MPS only, 1–2 s lag realistic, CC-BY-NC-4.0, 10+ GB peak, streaming server needed.
- **Qwen3-30B:** `/tmp/krab-ear-research/qwen3_30b_state.md` — lmstudio-community MLX-4bit 17.2 GB, 68–100 t/s on M4 Max, TTFT ~150–250 ms, **Instruct-2507** (non-thinking for voice), 119 languages trained.

---

## Total Effort Estimate

| PR | Title | Effort | Cumulative |
|----|-------|--------|-----------|
| 1.1 | Moshi + LazyEngine + WS | M (3-4d) | 3-4 d |
| 1.2 | SeamlessM4T + routing | M (2-3d) | 5-7 d |
| 1.3 | Krab Ear UI + WS client | M (3-4d) | 8-11 d |
| 1.4 | Krab agent handler | M (2-3d) | 10-14 d |
| 1.5 | Triggers | S (1-2d) | 11-16 d |
| 1.6 | LM Studio + eviction | XS (0.5d) | 11.5-16.5 d |
| 1.7 | XTTS (optional) | M (2d) | 13.5-18.5 d |
| 1.8 | E2E tests | M (3d) | 16.5-21.5 d |

**Total: 16.5–21.5 working days (~3–4 weeks calendar).** Parallelization (1.2 + 1.3 after 1.1) tightens to **11–16 d critical path**.

---

## Files & Directory Structure (Absolute Paths)

Per `/tmp/krab-ear-research/_phase1_file_structure.md`:

**Voice Gateway repo:** `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway`

**Krab Ear repo:** `/Users/pablito/Antigravity_AGENTS/Krab Ear`

**Krab agent repo:** `/Users/pablito/Antigravity_AGENTS/Краб`

All file paths in task bullets use these absolute roots.

