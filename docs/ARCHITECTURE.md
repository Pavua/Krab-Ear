# Krab Ear Architecture

> Structural reference for engineers. For operational procedures see `CLAUDE.md`.
> For IPC method catalogue see `docs/IPC_API_REFERENCE.md`.

---

## Overview

Krab Ear is a two-process local voice assistant for macOS. A Swift agent handles all
UI/system integration; a Python backend owns STT, history, and business logic. They
communicate exclusively over a Unix-domain socket using a JSON-RPC-like protocol.

```
  macOS system services
  ┌────────────────────────────────────────────────────────────────────────┐
  │  Accessibility API   ScreenCaptureKit   Notifications   Calendar.app  │
  └────┬───────────────────────┬─────────────────┬──────────────┬─────────┘
       │                       │                 │              │
  ┌────▼───────────────────────▼─────────────────▼──────────────▼─────────┐
  │                     Swift Agent (native/KrabEarAgent)                  │
  │  HotkeyManager  PasteService  BackendSupervisor  HealthMonitor         │
  │  HistoryPanelController (+ 20 extensions)        StatusIndicatorView   │
  │  RealtimeOverlayController  LiveSubtitlesOverlay  BackendToast         │
  │  ConversationViewController  SelectionTranslator  ErrorToastView       │
  │  IPCClient (sync JSON-RPC over Unix socket)                            │
  └──────────────────────────┬─────────────────────────────────────────────┘
                             │  Unix socket (JSON-RPC)
                             │  ~/Library/Application Support/KrabEar/krabear.sock  (production)
                             │  ~/.krab_ear_data/backend.sock                        (dev)
  ┌──────────────────────────▼─────────────────────────────────────────────┐
  │                     Python Backend (KrabEar/)                           │
  │  IPCServer → BackendService (dispatch, 324 handlers)                   │
  │  ├─ HistoryService         ├─ SettingsService    ├─ TranslationService  │
  │  ├─ CallAssistService      ├─ CallSessionService  ├─ LiveSubsService    │
  │  ├─ TTSService             └─ (+ 40 collaborators)                      │
  │  AudioEngine (STT chain)  StateStore  EventBus  ErrorBus               │
  └──────────────────────────┬─────────────────────────────────────────────┘
                             │
  ┌──────────────────────────▼─────────────────────────────────────────────┐
  │  External  │  LM Studio (LLMRewriter)   mlx-whisper / GigaAM / Parakeet│
  │  services  │  SenseVoice  pyannote.audio  Telnyx / Twilio  Sentry       │
  └────────────┘  openWakeWord  Telegram Bridge  Obsidian / Calendar.app   │
                  Voice Gateway WebSocket (ConversationVC)                 │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Native Agent (Swift)

**Package**: `native/KrabEarAgent/` — swift-tools-version 6.0, macOS 13+, single executable.
**Binary**: `Krab Ear.app/Contents/MacOS/KrabEarAgent` (production bundle).

### Entry points

| File | Responsibility |
|------|----------------|
| `main.swift` | App bootstrap, AppDelegate, RunLoop |
| `main+StatusMenu.swift` | NSStatusItem + menu construction |
| `main+HotkeyRecording.swift` | Right Option hotkey → start/stop recording |
| `main+PasteHandling.swift` | Transcription result → PasteService |
| `main+LiveSubs.swift` | SystemAudioCapture start/stop wiring |
| `main+Errors.swift` | SSE error stream → ErrorToastPresenter |
| `main+HealthMonitor.swift` | HealthMonitor actor start |
| `main+RealtimeOverlay.swift` | RealtimeOverlayController lifecycle |
| `main+IPCRecovery.swift` | IPC reconnect on backend restart |
| `main+QuickPresets.swift`, `main+QuickReplace.swift` | UI shortcuts |

### Core Swift classes

| Class | File | Purpose |
|-------|------|---------|
| `IPCClient` | `IPCClient.swift` | Sync JSON-RPC over Unix socket; used by all Swift callers |
| `BackendSupervisor` | `BackendSupervisor.swift` | Two-ring supervisor: exp backoff 0/2/5/15s + circuit breaker (5 fails/60s → 5 min cooldown) |
| `HealthMonitor` | `HealthMonitor.swift` | Actor; 3s ping `handle_ping`; 2 failures → SIGTERM→wait→SIGKILL→respawn |
| `HotkeyManager` | `HotkeyManager.swift` | CGEvent tap for Right Option; Right Option double-tap → voice assistant |
| `PasteService` | `PasteService.swift` | Accessibility API paste; Cmd+V fallback |
| `HistoryPanelController` | `HistoryPanelController.swift` + 20 extensions | Main UI: 3 tabs, 9 collapsible sections |
| `RealtimeOverlayController` | `RealtimeOverlayController.swift` | Floating live-transcription feedback HUD |
| `LiveSubtitlesOverlay` | `LiveSubtitlesOverlay.swift` | Floating HUD for live subs; last 3 lines, 4s auto-fade |
| `SelectionTranslator` | `SelectionTranslator.swift` | Cmd+Shift+T: AX read selection → translate → write back |
| `SystemAudioCapture` | `SystemAudioCapture.swift` | ScreenCaptureKit system audio tap → PCM base64 → IPC |
| `StatusIndicatorView` | `StatusIndicatorView.swift` | Menu bar dot: green/yellow/red by supervisor state |
| `BackendToast` | `BackendToast.swift` | Non-modal toast for backend events |
| `ErrorToastView` | `ErrorToastView.swift` | Severity-aware auto-dismiss panel (info 2s / warn 5s / error 10s / critical manual) |
| `ConversationViewController` | `ConversationViewController.swift` + 3 extensions | Voice assistant UI: WebSocket + audio |
| `SingleInstanceGuard` | `SingleInstanceGuard.swift` | Kills duplicate KrabEarAgent at launch |

---

## Layer 2: Backend (Python)

### BackendService (`KrabEar/backend/service.py`)

Central dispatcher. **5782 lines**, **324 IPC handlers** in the dispatch table. Owns
~40 collaborator instances created in `__init__`.

**Construction order** (key collaborators):
```
StateStore → VocabularyStore → AudioRecorder → LLMRewriter (+ warmup thread)
→ Transcriber → Translator → SettingsService → ErrorBus → LLMHttpProbe
→ HistoryService → CallAssistService → CallSessionService
→ LiveSubsService → TranslationService → TTSService
→ [40+ analytics / utility collaborators]
→ DiskSpaceMonitor.start() → RecapScheduler.start() [if enabled]
→ StartupDiagnostics.run_all_checks()
```

### Extracted sub-services

| Service | File | Handlers | LOC | Collaborators |
|---------|------|---------|-----|---------------|
| `HistoryService` | `history_service.py` | ~30 | 2867 | StateStore, SpeakerManager, LLMRewriter |
| `SettingsService` | `settings_service.py` | ~15 | 568 | StateStore (5s TTL cache) |
| `TranslationService` | `translation_service.py` | ~12 | 430 | Translator, StateStore, VocabularyStore |
| `CallAssistService` | `call_assist_service.py` | ~15 | 1111 | StateStore, AudioRecorder, Transcriber |
| `CallSessionService` | `call_session_service.py` | 6 | 185 | CallSessionStore, CallAutoEnd |
| `LiveSubsService` | `live_subs_service.py` | 3 | — | Transcriber, Translator, EventBus |
| `TTSService` | `tts_service.py` | 3 | — | Silero / Kokoro / macOS `say` |

### Future extractable services (identified tech debt)

| Candidate | Handler count | Est. LOC in service.py | Dominant handlers |
|-----------|--------------|------------------------|-------------------|
| **VocabularyService** | ~23 | ~800 | hotword_, stt_hotword_, vocabulary_, smart_vocabulary_, context_words, abbreviation_, auto_glossary_, glossary_, normalization_ |
| **ReportingService** | ~24 | ~600 | stats_report, digest, recap, html_report, keyword_cloud, timeline_, activity_calendar, analytics_dashboard, usage_stats, daily_cost |
| **AudioAnalyticsService** | ~14 | ~500 | analyze_*, audio_quality, sentiment, quality_trend, audio_fingerprint, speech_pace, waveform, silence_, word_timing |
| **IntegrationService** | ~13 | ~400 | obsidian_sync, calendar_link, webhook_, telegram_bridge, apple_note, apple_reminder, email, sharing, export_scheduler |

---

## Layer 3: Core Domain Logic (Python)

### Audio Engine (`core/engine.py` — 3014 LOC)

STT fallback chain: balanced model → max-candidates model → remote STT.
Post-processing: `TextUtils` cleanup (soft/strict profiles), LLMRewriter, diarization.

### Phase 4 Deterministic Pipeline (`core/pipeline/`)

Ordered stages executed by `PipelineExecutor`:

```
AudioNormalization → STT → TextCleanup → Diarization → Translation → LLMRewrite → Cache
```

Files: `executor.py`, `factory.py`, `context.py`, `stages/audio_normalization.py`,
`stages/stt.py`, `stages/text_cleanup.py`, `stages/diarization.py`,
`stages/translation.py`, `stages/llm_rewrite.py`, `stage_cache.py`.

STT adapters: `stt_whisper_mlx_adapter.py`, `stt_gigaam_adapter.py`,
`stt_sensevoice.py`, `stt_parakeet.py` (router: `stt_router.py`).

### Key core modules

| Module | Class | Notes |
|--------|-------|-------|
| `core/config.py` | `settings` | Pydantic-Settings singleton; all params via `KRAB_EAR_*` env vars |
| `core/utils.py` | `TextUtils` | Transcript cleanup, hallucination stripping, dedup |
| `core/mlx_lock.py` | `mlx_lock()` | Global RLock — ALL MLX inference must hold this lock (SIGSEGV prevention) |
| `core/mlx_inter_lock.py` | `mlx_inter_process_lock()` | POSIX `flock` cross-process MLX serialization |
| `core/mlx_subprocess.py` | — | MLX watchdog subprocess with timeout + auto-recovery |
| `core/engine.py` | `AudioEngine` | STT chain + diarization + TTS |
| `core/stt_router.py` | `STTRouter` | Language-aware adapter selection with fallback |
| `core/transcript_context.py` | `TranscriptContext` | Builds Whisper `initial_prompt` from recent history + hotwords |
| `core/text_postprocessor.py` | `TextPostProcessor` | Configurable post-processing pipeline |
| `core/punctuation_fixer.py` | `PunctuationFixer` | Rule-based RU/ES punctuation correction |

---

## Layer 4: Infrastructure

### Module Dependency Graph

```
BackendService
  ├── StateStore ──────────────── history.ndjson (append-only NDJSON)
  │   └── (file lock via fcntl)   settings.json (runtime, not in repo)
  ├── EventBus ────────────────── pub/sub + SSE stream (port exposed via IPCServer)
  │   └── emit(type, data)        consumed by Swift SSESessionDelegate
  ├── ErrorBus ────────────────── KrabError (Pydantic) + ring buffer (200)
  │   ├── ERROR_REGISTRY          24 error codes → Sentry tier routing
  │   └── WarnBatcher             dedupe window 30s
  ├── IPCServer ───────────────── Unix socket, JSON-RPC envelope
  │   └── handle_request()        dispatch table lookup → delegate to sub-service
  ├── LLMRewriter ─────────────── LM Studio REST (qwen/gemma models)
  │   ├── CircuitBreaker          5 failures → open
  │   └── LLMHttpProbe            passive GET /v1/models every 30s
  ├── AudioEngine
  │   ├── mlx_lock ────────────── RLock (intra-process MLX serialization)
  │   └── mlx_inter_process_lock  flock (cross-process)
  └── SemanticSearcher ────────── multilingual-e5-base embedding index (opt-in)
```

### Persistence files (runtime data, not in repo)

| File | Owner | Format |
|------|-------|--------|
| `history.ndjson` | StateStore | Append-only, tombstone deletes, compaction |
| `archive.ndjson` | ArchiveManager | Older items moved from history |
| `settings.json` | SettingsService | Runtime settings (5s TTL cache) |
| `call_sessions.ndjson` | CallSessionStore | Call lifecycle records |
| `event_replay.ndjson` | EventReplayManager | Persisted event log for replay |
| `obsidian_sync.json` | ObsidianSyncManager | Incremental sync state |
| `vocabulary.json` | VocabularyStore | User-defined STT hotwords |

---

## Service Extraction Pattern

How to extract a handler cluster from `BackendService` into a new service:

```
1. Identify cluster
   grep '"method_name": self\.' service.py
   Group handlers by shared collaborator / domain noun.

2. Create KrabEar/backend/new_service.py
   class NewService:
       def __init__(self, store: StateStore, <specific collaborators>) -> None:
           ...
       def handle_<method>(self, params: dict) -> dict:
           ...

3. Wire in BackendService.__init__
   from backend.new_service import NewService
   self._new_svc = NewService(store=self.store, ...)

4. Delegate in dispatch table (handle_request)
   "method_name": self._new_svc.handle_method_name,

5. Add unit tests
   KrabEar/tests/test_new_service.py
   Stub collaborators (FakeStateStore / in-memory dict).

6. Remove old _handle_* methods from service.py
   Verify test suite still passes: make test
```

**Reference implementation**: `CallSessionService` (PR #420) — 185 LOC, 6 handlers,
2 collaborators (`CallSessionStore`, `CallAutoEnd`). Start there.

**Extraction priority order** (smallest risk first):
1. `VocabularyService` — purely reads/writes VocabularyStore, no recorder/transcriber deps
2. `AudioAnalyticsService` — stateless analyzers (no persistent store)
3. `ReportingService` — stateless generators, easy to unit-test
4. `IntegrationService` — external-service wrappers, each independent

---

## Important Patterns

### MLX thread-safety

**MLX is not thread-safe.** Concurrent GPU access causes SIGSEGV in
`__hash_table<MTL::Resource*>`. All MLX inference must be serialized:

```python
from core.mlx_lock import mlx_lock

with mlx_lock():
    result = mlx_whisper.transcribe(audio, ...)
```

PyTorch+MPS adapters (SenseVoice, Parakeet, GigaAM, WhisperX) do NOT need this lock.

### Runtime settings reads

**Always use `_get_runtime_setting(key, default)`** for startup-time reads of
user-overridable settings. `DEFAULT_SETTINGS.get(key)` reads the static dict from
module load time and ignores `settings.json` runtime overrides (Wave 58 root cause).

```python
# WRONG — ignores runtime settings.json
timeout = DEFAULT_SETTINGS.get("rewriter_warmup_timeout_sec", 60)

# CORRECT — reads settings.json first, static dict as ultimate fallback
timeout = self._get_runtime_setting("rewriter_warmup_timeout_sec", 60)
```

### Caches

| Cache | Location | TTL / Policy |
|-------|----------|-------------|
| Settings | `SettingsService._cache` | 5s TTL (invalidated on save) |
| AudioLanguageID | `AudioLanguageID` | LRU-1 (Wave 63 — prevents model reload per chunk) |
| Translation | `TranslationCache` | Persistent on-disk (exact-match key) |
| Semantic search index | `SemanticSearcher` | Disk-backed embedding index (lazy load) |
| Auto-glossary | `AutoGlossaryBuilder` | Refresh every N hours (configurable) |

### Background threads / actors

| Thread/Actor | Started by | Purpose |
|-------------|-----------|---------|
| `stt-warmup` | `BackendService.__init__` | Pre-loads Whisper model before first dictation |
| `rewriter-warmup` | `BackendService.__init__` | Pre-loads LM Studio model |
| `DiskSpaceMonitor` | `BackendService.__init__` | Warns when free space < 2 GB |
| `RecapScheduler` | `BackendService.__init__` (if enabled) | Daily email digest |
| `AutoBackupManager` | `BackendService.__init__` | Rolling backups |
| `LLMHttpProbe` | `BackendService.__init__` | GET /v1/models every 30s; fires error events |
| `HealthMonitor` (Swift actor) | `main+HealthMonitor.swift` | 3s ping; SIGTERM+SIGKILL on 2 misses |
| `BackendSupervisor` (Swift) | `AppDelegate` | Exp backoff restart + circuit breaker |

### Singletons

- `settings` (`core/config.py`) — Pydantic-Settings, process-level, env-var overridable
- `event_bus` (`backend/event_bus.py`) — module-level singleton, accessed by `from backend.event_bus import event_bus`
- `error_bus` — created once in `BackendService.__init__`, passed to collaborators

---

## Recent Waves (May 2026)

| Wave | Key change | Files |
|------|-----------|-------|
| Wave 42 | AGENT-H AppHang fix (`showFatalAndTerminate` → async) | `BackendSupervisor.swift` |
| Wave 50 | Self-recovery critical bug fix (`pgrep + set -e` never triggered) | `BackendSupervisor.swift` |
| Wave 58 | Runtime-vs-static settings drift fix (70 silent warmup timeouts/day) | `service.py` lines 187, 229 |
| Wave 59 | AGENT-J fix, supervisor cooldown, agent launchd plist | `BackendSupervisor.swift`, `scripts/` |
| Wave 63 | MLX memory leak fix (`mx.clear_cache`), LRU-1 AudioLanguageID | `core/audio_lang_id.py` |
| Wave 64 | stt.gigaam.ffmpeg_missing startup error | `service.py` |
| Wave 65 | Dead handler cleanup batch 1 (19 removed), `CallSessionService` extraction | `service.py`, `call_session_service.py` |
| Wave 66 | 5 silent Swift IPC bugs (call_dial → call_session_create, etc.) | `CallAutomationController.swift` |

---

## CI / Release

```
GitHub Actions (.github/workflows/ci.yml)
  ├── Python tests: pytest KrabEar/tests/ (-v, PYTHONPATH set)
  └── Swift build: swift build -c release (native/KrabEarAgent)

Release steps (make release):
  1. swift build -c release
  2. cp .build/release/KrabEarAgent → Krab Ear.app/Contents/MacOS/
  3. codesign -s "Krab Ear Dev Local" -f ...  (stable identity — TCC survives rebuild)
  4. cp bundle binary → native/runtime/KrabEarAgent  (two-binary sync — avoid drift!)
  5. dSYM upload to Sentry (krab-ear-agent project)
  6. Bump CFBundleVersion → Sentry release tag

launchd agents (production):
  com.antigravity.krab-ear.backend  — Python IPC backend (KeepAlive=true, Variant B)
  com.antigravity.krab-ear.rest     — Flask REST API (port 5005, separate)
  ai.krab.ear.agent                 — Swift agent (Wave 59, opt-in via install script)
```

**Two-binary drift**: `Krab Ear.app/Contents/MacOS/KrabEarAgent` (bundle, Dock/launchd)
and `native/runtime/KrabEarAgent` (login session) must be kept in sync. Divergence
causes "backend недоступен" symptoms. Always sync both after rebuild (see `make sign`).

---

## Cross-references

- Full IPC method catalogue: `docs/IPC_API_REFERENCE.md` (4341 lines, 241 handlers documented)
- Operational procedures, TCC troubleshooting, sub-agent model routing: `CLAUDE.md`
- User-facing guide: `docs/USER_MANUAL.md`
- Sentry observability: see `docs/archive/2026-05-26-pre-marathon/RUNBOOK.md` (archived; current procedures in CLAUDE.md)
- Distribution build: `docs/DISTRIBUTION.md`
- Dev codesign identity: `docs/DEV_CODESIGN.md`
