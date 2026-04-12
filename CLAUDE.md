# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Krab Ear is a local voice assistant/transcriber for macOS. It runs as a two-process system:
- **Native Swift agent** (`native/KrabEarAgent/`) — handles global hotkey (Right Option), UI panel, accessibility paste, and supervises the Python backend via Unix socket IPC.
- **Python backend** (`KrabEar/`) — performs offline STT via `mlx-whisper`, speaker diarization via `pyannote.audio`, translation, and manages transcription history.

The project is bilingual (RU/ES primary, EN secondary). Code comments, UI labels, and docs are in Russian.

## Architecture

```
┌─────────────────────────┐    Unix socket (JSON-RPC)    ┌──────────────────────┐
│  Swift Agent (macOS)    │ ◄────────────────────────── ►│  Python Backend      │
│  - HotkeyManager        │                              │  - IPCServer         │
│  - PasteService         │                              │  - BackendService    │
│  - HistoryPanel         │    Krab Ear.app/             │    → CallAssistSvc   │
│  - BackendSupervisor    │    (bundle wraps agent       │    → HistorySvc      │
│  - KrabEarTheme         │     + Python venv)           │    → TranslationSvc  │
│  - CollapsibleSection   │                              │    → SettingsSvc     │
│  - RealtimeOverlay      │                              │  - AudioRecorder     │
│  - NotificationService  │                              │  - Transcriber       │
│  - LaunchAgentManager   │                              │  - Translator        │
│  - SystemAudioDucking   │                              │  - LLMRewriter       │
│                         │                              │  - StateStore (NDJSON)│
│                         │                              │  - MetricsCollector  │
│                         │                              │  - VGWSClient        │
└─────────────────────────┘                              └──────────────────────┘
```

### Key layers inside `KrabEar/`:
- **`core/config.py`** — Pydantic-Settings singleton (`settings`), all params overridable via `KRAB_EAR_*` env vars. Also contains `DEFAULT_SETTINGS` dict used by UI/IPC.
- **`core/engine.py`** — `AudioEngine`: STT via mlx-whisper with fallback chain (balanced → max candidates → remote), audio normalization, diarization pipeline (pyannote), TTS via macOS `say`.
- **`core/utils.py`** — `TextUtils`: transcript cleanup (soft/strict profiles), hallucination stripping, phrase dedup.
- **`backend/service.py`** — `BackendService` (business logic) + `IPCServer` (Unix socket server). Single file, ~1969 lines. The `handle_request` method dispatches JSON-RPC methods, delegating to 4 extracted services.
- **`backend/call_assist_service.py`** — `CallAssistService`: call assist delegation, VoiceGatewayClient integration.
- **`backend/history_service.py`** — `HistoryService`: history CRUD, SRT export, clipboard history, storage info.
- **`backend/translation_service.py`** — `TranslationService`: translate, glossary management, vocabulary suggestions.
- **`backend/settings_service.py`** — `SettingsService`: settings CRUD, profile presets, 5s TTL cache.
- **`backend/recorder.py`** — `AudioRecorder`: thread-safe start/stop audio capture via `sounddevice`.
- **`backend/state_store.py`** — `StateStore`: append-only NDJSON history with tombstone deletes, file-lock, and compaction. Settings stored as `settings.json`.
- **`backend/transcriber.py`** — Thin wrapper over `AudioEngine` for profile/vocabulary management.
- **`backend/translator.py`** — Offline-first translator (RU↔ES, EN→RU, Auto, Bilingual modes) with in-memory cache.
- **`backend/llm_rewriter.py`** — LLM post-processing via LM Studio (qwen3-4b-abliterated). CircuitBreaker + chatbot detection + length ratio guard.
- **`backend/rest_server.py`** — Flask REST API (port 5005) for HTTP-based transcription and metrics. Separate from the IPC service.
- **`backend/event_bus.py`** — In-process pub/sub EventBus with SSE streaming. Supports both untyped `emit(str, dict)` and typed `emit_typed(EventType, BaseModel)`.
- **`backend/metrics_collector.py`** — Thread-safe sliding-window metrics (latency percentiles, confidence).
- **`contracts/`** — Pydantic models for event payloads (STT, Translation). `EventType` enum + `EVENT_SCHEMA_MAP` for runtime dispatch. JSON Schema export via `python -m contracts.export`.

### Native agent (`native/KrabEarAgent/`):
- Swift Package (swift-tools-version 6.0, macOS 13+). Single executable target.
- Communicates with backend exclusively through Unix socket JSON-RPC.
- Resolves project root by checking for `KrabEar/backend/service.py`.
- **`KrabEarTheme.swift`** — Liquid Glass visual theme (NSVisualEffectView). ThemeCardView, CollapsibleSectionView, ThemePrimaryButton.
- **`HistoryPanelController.swift`** (2196 lines) + 7 extension files: `+CallAssist`, `+Diagnostics`, `+History`, `+HistoryEnhancements`, `+Import`, `+Settings` (split for maintainability).
- **`RealtimeOverlayController.swift`** — floating overlay for live transcription feedback.
- **`NotificationService.swift`** — macOS user notifications (confidence warnings, errors).
- **`LaunchAgentManager.swift`** — install/remove launchd plist for auto-start.
- **`SystemAudioDuckingService.swift`** — lower system volume during recording.
- **`PermissionWizard.swift`** — guided Accessibility + Microphone permission setup.

### `.app` bundle (`Krab Ear.app/`):
- Standard macOS app bundle (`com.antigravity.krab-ear`, LSUIElement=true, macOS 13+).
- `Contents/MacOS/KrabEarAgent` is the compiled Swift binary (same as `native/runtime/KrabEarAgent`).
- Build and install into bundle:
  ```bash
  cd native/KrabEarAgent && swift build -c release
  cp -f .build/release/KrabEarAgent "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
  codesign -s - -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
  ```

## Krab ecosystem start/stop (НЕ трогать из этого проекта!)

Основной Краб (Telegram userbot) запускается/останавливается ТОЛЬКО через:
```bash
/Users/pablito/Antigravity_AGENTS/new\ start_krab.command   # СТАРТ
/Users/pablito/Antigravity_AGENTS/new\ Stop\ Krab.command    # СТОП
```

**ЗАПРЕЩЕНО:** `kill -9`, `SIGHUP`, `Restart Krab.command`, прямой `python -m src.main`.
OpenClaw Gateway: `openclaw gateway stop && sleep 2 && openclaw gateway start` (НЕ SIGHUP).

## Common Commands

### Python backend

```bash
# Activate virtualenv
source .venv_krab_ear/bin/activate

# Run IPC backend service
python KrabEar/main.py --data-dir ~/.krab_ear_data

# Run REST server (port 5005)
PYTHONPATH=$PYTHONPATH:$(pwd)/KrabEar python KrabEar/backend/rest_server.py

# Install dependencies
pip install -r KrabEar/requirements.txt
```

### Tests

```bash
# Run all tests (from repo root, with PYTHONPATH set)
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v

# Run a single test file
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_engine_cleanup.py -v

# Run a single test case
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_engine_cleanup.py::EngineCleanupTestCase::test_removes_repeated_last_sentence -v

# Tests can also be run via unittest directly
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_backend_service.py -v
```

Tests use `unittest.TestCase` with fake/stub collaborators (e.g., `FakeRecorder`, `FakeTranscriber`). Integration tests create temp directories for `StateStore`. No external services required for test suite. Current count: 411 tests.

### Swift agent

```bash
# Build native agent
cd native/KrabEarAgent && swift build -c release

# The compiled binary goes to native/runtime/KrabEarAgent

# Rebuild + sign Swift agent (full cycle)
cd native/KrabEarAgent && swift build -c release && cp -f .build/release/KrabEarAgent ../runtime/KrabEarAgent && codesign -s - -f ../runtime/KrabEarAgent
```

### Makefile shortcuts

```bash
# Use Makefile for common operations
make test          # Run all Python tests
make build         # Build Swift agent
make sign          # Build + copy + codesign
make lint          # Flake8 on Python backend
```

### One-click shortcuts
- `Start Krab Ear.command` — full launch (venv setup + agent start)
- `Update Krab Ear Agent.command` — rebuild Swift agent only
- `start_rest_service.command` — launch Flask REST API

## Important Patterns

- **IPC protocol**: JSON-RPC-like over Unix socket. Path depends on how backend was launched:
  - **Production (launchd Variant B, see `scripts/install_backend_launchagent.command`)**: `~/Library/Application Support/KrabEar/krabear.sock`
  - **Dev standalone** (`python KrabEar/main.py --data-dir ~/.krab_ear_data`): `~/.krab_ear_data/backend.sock`
  
  Request format: `{"id": "...", "method": "...", "params": {...}}`. Response: `{"id": "...", "ok": true, "result": {...}}`.
- **History storage**: Append-only NDJSON (`history.ndjson`) with tombstone-based deletes and periodic compaction. All writes are file-lock protected.
- **STT fallback chain**: balanced model → max model candidates → remote STT (if network mode allows). Unavailable models are tracked in `_unavailable_models` set.
- **LLM post-processing**: engine.py hooks into LLMRewriter after STT, before paste. Chatbot guard rejects responses starting with known assistant phrases. Length ratio guard rejects output <35% or >300% of input.
- **Collapsible GUI sections**: CollapsibleSectionView with UserDefaults persistence (key: `CollapsibleSection_{sectionId}`). Disclosure triangle toggle with animation.
- **iCloud audio import**: files from `Mobile Documents/com~apple~CloudDocs` are auto-copied to /tmp before ffmpeg (errno 11 workaround).
- **Transcript files**: imported audio generates .md files in `~/Library/Application Support/KrabEar/transcripts/`.
- **Legacy compatibility**: `AudioEngine` has static method aliases (`_cleanup_soft`, `_normalize_phrase`, etc.) that delegate to `TextUtils` — these exist for backwards compatibility with older tests.
- **Config override**: Any setting in `core/config.py` can be overridden via `KRAB_EAR_<SETTING_NAME>` environment variable.
- **Test path setup**: Test files manually prepend `PROJECT_ROOT` to `sys.path` to resolve `backend.*` and `core.*` imports when run standalone.
- **Event contracts**: All events use `{type, ts, data}` envelope (EVENT_CONTRACT_V1). Event types are defined in `contracts/registry.py`. Each service owns its event schemas — Krab Ear owns STT + Translation, Voice Gateway owns TTS + Session.
- **Release process**: `RELEASE_CHECKLIST.md` at repo root. Automated part via `scripts/run_release_checklist.command`.
- **Service extraction pattern**: each extracted service takes `store` + specific collaborators in its constructor; handler methods named `handle_*`; `BackendService` imports the service and delegates matching IPC methods to it.
- **Dead code removal workflow**: extract logic into new service → add delegation calls in `BackendService.handle_request` → verify all tests pass → remove original methods from `BackendService`.
- **CallAssistService delegation**: `HistoryPanelController+CallAssist.swift` delegates all call assist logic to `CallAssistService` (Python backend); Swift side is thin UI/IPC glue only.
- **JSON structured logging**: `LOG_FORMAT` setting (`json` or `text`). When set to `json`, all backend log output uses structured JSON lines for easier parsing/filtering.
- **GitHub Actions CI**: `.github/workflows/ci.yml` runs Python tests (pytest) and Swift build on every push/PR.
- **Profile presets**: four built-in presets (`default`, `meeting`, `translation`, `call_recording`) applied via `apply_profile_preset` IPC method. `list_profile_presets` returns their names/descriptions.
- **Diagnostics**: `get_diagnostics` IPC method returns a structured dict with sections: `system`, `stt`, `llm`, `history`, `settings_cache`. Use for debug panels and status reporting.
- **Metrics dashboard**: `get_metrics_dashboard` returns sliding-window latency percentiles, confidence stats, and diarization usage rate from `MetricsCollector`.
- **Audio device management**: `list_audio_inputs` / `get_audio_devices` enumerate sounddevice inputs; `test_microphone` records a short clip and returns RMS/peak levels. Used by GUI audio device picker.
- **Clipboard history**: last 20 paste items stored in memory; `get_clipboard_history` / `repaste_item` IPC methods. `cleanup_old_history` deletes NDJSON entries older than N days; `get_storage_info` returns file sizes.
- **SRT export**: `export_history_srt` IPC method exports history items as SubRip subtitle file.
- **Glossary auto-learn**: `get_glossary_suggestions` proposes new glossary entries from recent transcripts; `set_translation_glossary_item` / `remove_translation_glossary_item` manage the glossary.
- **Vocabulary suggestions**: `get_vocabulary_suggestions` proposes STT vocabulary entries from recent history.
- **Diarization on Metal GPU**: pyannote.audio + torch 2.11, device selected via `diarization_device` setting (auto-selects `mps` on Apple Silicon when available).
- **GUI layout**: 3 tabs (Main, History, Settings) with 9 total collapsible sections. Tab state is persisted via NSUserDefaults.
- **Call Assist**: `start_call_assist` / `stop_call_assist` and related IPC methods (`call_assist_diagnostics`, `call_assist_summary`, `call_assist_timeline_*`, `call_assist_quick_phrase`) manage a real-time call translation/assist session.

## Non-goals (from PRD)

- Merging Krab/Ear/Voice into a single runtime — they remain separate projects with API boundaries.
- Krab Ear does not implement web scraping; external tool/reasoning goes through OpenClaw gateway.
