# Phase 1 (Voice Assistant Mode) — File Structure Mapping

Pre-plan reference. Files to be created/modified across 3 repos.

## Repo 1: Krab Voice Gateway (`/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway`)

### NEW files (PR 1.1, 1.2)
```
app/conversation/
  __init__.py                # Module exports
  base.py                    # LazyConversationEngine ABC + EngineState dataclass
  moshi_engine.py            # MoshiEngine (mlx-moshiko wrapper)
  seamless_engine.py         # SeamlessM4TEngine (Meta wrapper)
  language_detect.py         # detect_language(audio_bytes) -> str
  brain_proxy.py             # AsyncBrainProxy → calls Krab agent /v1/voice
  ws_handler.py              # ConversationWSHandler (FastAPI WebSocket)
  session_state.py           # ConversationSessionState (in-memory per session)
  events.py                  # JSON event protocol (stt.partial, engine.loaded, etc.)
  exceptions.py              # ConversationError taxonomy

tests/conversation/
  __init__.py
  test_base.py               # Lazy load lifecycle, LRU eviction
  test_moshi_engine.py       # FakeMoshi mock; full-duplex chunked I/O
  test_seamless_engine.py    # FakeSeamless mock; multilingual routing
  test_language_detect.py    # heuristic + real model sniff
  test_brain_proxy.py        # mock Krab agent HTTP server
  test_ws_handler.py         # WebSocket protocol roundtrip
  test_session_state.py      # state transitions, cleanup
```

### MODIFY (PR 1.1, 1.2)
```
app/main.py                  # Mount /v1/sessions/{id}/conversation WS endpoint
app/config.py                # Add KRAB_VG_MOSHI_MODEL, KRAB_VG_SEAMLESS_MODEL,
                             #   KRAB_VG_BRAIN_URL, KRAB_VG_LRU_TTL_SEC
requirements.txt             # + mlx-moshi, transformers, sentencepiece (Seamless deps)
```

## Repo 2: Krab Ear (`/Users/pablito/Antigravity_AGENTS/Krab Ear`)

### NEW Swift files (PR 1.3, 1.5)
```
native/KrabEarAgent/Sources/KrabEarAgent/
  ConversationViewController.swift              # NSViewController root
  ConversationViewController+UI.swift           # Waveform, transcript, controls
  ConversationViewController+WebSocket.swift    # URLSessionWebSocketTask
  ConversationViewController+Audio.swift        # AVAudioEngine capture/playback
  ConversationViewController+Triggers.swift     # GUI button + hotkey + wake word
  WakeWordListener.swift                        # Silero CoreML wakeword detection
  HotkeyDoubleTapDetector.swift                 # Right Option double-tap (300ms window)
  ConversationEvents.swift                      # Decoded JSON event types from VG
  HistoryPanelController+VoiceTab.swift         # Tab integration in main panel
```

### NEW Resources (PR 1.5)
```
native/KrabEarAgent/Resources/
  silero_wakeword_kr.mlmodelc/                  # Compiled CoreML model для "Краб"
```

### NEW Tests (PR 1.3)
```
native/KrabEarAgent/Tests/KrabEarAgentTests/
  ConversationViewControllerTests.swift         # Mock WS server, UI state
  WakeWordListenerTests.swift                   # Mock audio buffer
```

### MODIFY (PR 1.3, 1.5)
```
native/KrabEarAgent/Sources/KrabEarAgent/
  HistoryPanelController.swift                  # Add 4th tab "Разговор с AI"
  HistoryPanelController+Settings.swift         # Add wake-word toggle, hotkey config
  HotkeyManager.swift                           # Hook to ConversationViewController
  Models.swift                                  # ConversationConfig struct
  KrabEarTheme.swift                            # ThemeWaveformView (новый component)
KrabEar/backend/service.py                      # Add IPC method get_voice_config (UI config)
KrabEar/core/config.py                          # VOICE_GATEWAY_URL уже есть; add fallback URL
```

## Repo 3: Krab agent (`/Users/pablito/Antigravity_AGENTS/Краб`)

### NEW Python files (PR 1.4)
```
src/voice_channel/
  __init__.py
  voice_channel_handler.py     # Coordinates voice → OpenClaw → response
  voice_routes.py              # FastAPI routes: POST /v1/voice/message
  voice_state.py               # Per-conversation state (transcript buffer)

tests/voice_channel/
  __init__.py
  test_voice_channel_handler.py
  test_voice_routes.py
```

### NEW MCP tools (PR 1.4)
```
src/mcp_tools/
  voice_assistant_tools.py     # MCP tools: get_recent_dictations, transcribe_file,
                               #   send_telegram, search_memory, etc.
```

### MODIFY (PR 1.4, 1.6)
```
src/openclaw_client.py         # Add tool registry hook for voice_assistant_*
src/model_manager.py           # Hint param "voice_assistant" → prefer fast model
src/userbot_bridge.py          # Hook for voice channel parallel to Telegram
src/config.py                  # KRAB_VOICE_PORT (default 8081)
src/mcp_client.py              # Register voice_assistant_tools
```

## Cross-repo dependency graph

```
PR 1.1 (Voice Gateway: Moshi + base)
  └─ depends on: nothing
  └─ unblocks: PR 1.2, PR 1.3, PR 1.4, PR 1.5

PR 1.2 (Voice Gateway: SeamlessM4T)
  └─ depends on: PR 1.1 (LazyConversationEngine base)
  └─ unblocks: PR 1.6 (final brain integration)

PR 1.3 (Krab Ear UI: ConversationViewController)
  └─ depends on: PR 1.1 (WS protocol contract)
  └─ unblocks: PR 1.5

PR 1.4 (Krab agent: voice_channel_handler + brain proxy)
  └─ depends on: PR 1.1 (BrainProxy contract)
  └─ unblocks: PR 1.6

PR 1.5 (Triggers)
  └─ depends on: PR 1.3 (controller exists)

PR 1.6 (qwen3-30b setup + routing)
  └─ depends on: PR 1.4 (brain proxy)

PR 1.7 (XTTS-v2 fallback) — OPTIONAL
  └─ depends on: PR 1.2

PR 1.8 (E2E acceptance)
  └─ depends on: ALL above
```

## Estimated PR count + effort

| PR | Title | Files NEW | Files MOD | Tests | Effort |
|----|-------|-----------|-----------|-------|--------|
| 1.1 | Voice Gateway: Moshi engine + base | 9 | 3 | 4 | M (3-4 days) |
| 1.2 | Voice Gateway: SeamlessM4T engine | 1 | 1 | 1 | M (2-3 days) |
| 1.3 | Krab Ear: ConversationViewController + WS | 7 | 4 | 2 | M (3-4 days) |
| 1.4 | Krab agent: voice_channel_handler | 5 | 5 | 2 | M (2-3 days) |
| 1.5 | Triggers + Silero wake word | 4 | 3 | 1 | S (1-2 days) |
| 1.6 | qwen3-30b LM Studio setup + LRU | 0 | 3 | 1 | XS (0.5 day) |
| 1.7 | XTTS-v2 voice clone (optional) | 1 | 1 | 0 | M (2 days) |
| 1.8 | E2E acceptance + integration tests | 1 | 0 | 8 | M (3 days) |

**Total: ~17-22 working days** depending on PR 1.7 inclusion. ~3-4 weeks calendar.
