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
- **`backend/service.py`** — `BackendService` (business logic) + `IPCServer` (Unix socket server). Single file, ~3451 lines. The `handle_request` method dispatches 195 JSON-RPC methods via a handler lookup table, delegating to extracted services.
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
- **`backend/obsidian_sync.py`** — `ObsidianSyncManager`: sync transcriptions to an Obsidian vault as .md files with YAML frontmatter; incremental (timestamp-based) and forced modes; state persisted in `obsidian_sync.json`.
- **`backend/sentiment_trends.py`** — `SentimentTrendAnalyzer`: daily sentiment aggregation over history items using `EmotionDetector`; linear-regression mood trend (`improving`/`stable`/`declining`).
- **`backend/collection_manager.py`** — `CollectionManager`: named collections of history items; CRUD + bulk operations.
- **`backend/daily_digest.py`** — `DailyDigestGenerator`: daily summary digest of transcription activity.
- **`backend/integrity_checker.py`** — `IntegrityChecker`: NDJSON integrity validation and repair for history store.
- **`backend/period_comparison.py`** — `PeriodComparator`: compare transcription statistics across arbitrary time periods.
- **`backend/quality_trends.py`** — `QualityTrendAnalyzer`: track confidence/quality trends over time.
- **`backend/speaker_manager.py`** — `SpeakerManager`: persistent speaker profiles and rename/merge for diarization output.
- **`core/punctuation_fixer.py`** — `PunctuationFixer`: rule-based Russian/Spanish punctuation correction.
- **`core/term_extractor.py`** — `TermExtractor`: keyword/term extraction from transcripts for vocabulary and glossary suggestions.
- **`core/text_comparator.py`** — `TextComparator`: structural diff/similarity scoring between two transcript texts.
- **`backend/analytics_dashboard.py`** — `AnalyticsDashboard`: aggregate all analytics metrics into a single dashboard snapshot.
- **`backend/audit_logger.py`** — `AuditLogger`: structured audit trail for IPC operations.
- **`backend/auto_backup.py`** — `AutoBackupManager`: scheduled background backups with configurable interval and copy limit.
- **`backend/config_presets_library.py`** — `ConfigPresetsLibrary`: built-in + custom config presets for quick settings switching.
- **`backend/cost_estimator.py`** — `CostEstimator`: compute cost estimation (CPU time, memory, disk) per recording.
- **`backend/data_migrator.py`** — `DataMigrator`: versioned data migration between schema versions.
- **`backend/error_reporter.py`** — `ErrorReporter`: ring-buffer error aggregation with per-component/type counts.
- **`backend/event_replay.py`** — `EventReplayManager`: persist and replay event log entries; supports time-range replay.
- **`backend/export_scheduler.py`** — `ExportScheduler`: scheduled auto-export to file on configurable interval.
- **`backend/feature_flags.py`** — `FeatureFlags`: runtime on/off flags for experimental features.
- **`backend/hotword_detector.py`** — `HotwordDetector`: scan transcripts for trigger words.
- **`backend/html_report.py`** — `HtmlReportGenerator`: standalone HTML analytics report.
- **`backend/input_sanitizer.py`** — `InputSanitizer`: validate and sanitize IPC params.
- **`backend/ipc_throttle.py`** — `IPCThrottle`: per-method rate limiting (token bucket) for heavy IPC calls.
- **`backend/keyword_cloud.py`** — `KeywordCloudGenerator`: word-cloud data (count, weight, font_size) from history.
- **`backend/language_learning.py`** — `LanguageLearningManager`: bilingual vocabulary extraction and flashcard generation.
- **`backend/model_cache_manager.py`** — `ModelCacheManager`: HuggingFace model cache management.
- **`backend/performance_profiler.py`** — `PerformanceProfiler`: elapsed-time profiling for backend operations.
- **`backend/period_comparison.py`** — `PeriodComparator`: compare transcription statistics across arbitrary time periods. *(listed above)*
- **`backend/playback_tracker.py`** — `PlaybackTracker`: persistent playback event tracking (play count, total listened).
- **`backend/plugin_system.py`** — `PluginSystem`: simple plugin loader for extensibility.
- **`backend/recording_chain.py`** — `RecordingChainManager`: link related recordings into ordered chains.
- **`backend/recording_comparison.py`** — `RecordingComparison`: side-by-side multi-recording comparison (similarity matrix, shared words).
- **`backend/recording_insights.py`** — `RecordingInsightsGenerator`: heuristic insight generation from recording patterns.
- **`backend/recording_merger.py`** — `RecordingMerger`: merge multiple history items into a single item.
- **`backend/recording_scheduler.py`** — `RecordingScheduler`: schedule future recordings with start time and duration.
- **`backend/request_signing.py`** — `RequestSigner`: HMAC-SHA256 request authentication for IPC.
- **`backend/sentiment_trends.py`** — `SentimentTrendAnalyzer`: daily sentiment aggregation with linear-regression mood trend. *(listed above)*
- **`backend/sharing_manager.py`** — `SharingManager`: create and retrieve shareable transcript packages.
- **`backend/smart_vocabulary.py`** — `SmartVocabularyBuilder`: pattern-based vocabulary suggestions and auto-update from history.
- **`backend/speaker_statistics.py`** — `SpeakerStatisticsAnalyzer`: per-speaker word count, duration, confidence from diarized history.
- **`backend/startup_diagnostics.py`** — `StartupDiagnostics`: run all readiness checks at startup; report status.
- **`backend/summary_profiles.py`** — `SummaryProfileManager`: custom summarization profiles for LLM batch summaries.
- **`backend/system_monitor.py`** — `SystemMonitor`: real-time CPU, RAM, disk, GPU monitoring.
- **`backend/template_manager.py`** — `TemplateManager`: user-defined text output templates.
- **`backend/timeline_view.py`** — `TimelineViewGenerator`: topic-shift timeline from history items.
- **`backend/transcript_versioning.py`** — `TranscriptVersionManager`: full version history for individual transcript texts.
- **`backend/transcription_queue.py`** — `TranscriptionQueue`: priority queue for batch audio file transcription jobs.
- **`backend/translation_cache.py`** — `TranslationCache`: persistent on-disk translation result cache.
- **`backend/usage_tracker.py`** — `UsageTracker`: daily usage statistics (recordings, duration, words).
- **`backend/vocabulary_store.py`** — `VocabularyStore`: persist user-defined STT vocabulary words to disk.
- **`backend/webhook_manager.py`** — `WebhookManager`: register and fire HTTP webhooks on IPC events.
- **`core/abbreviation_expander.py`** — `AbbreviationExpander`: expand RU/ES/EN abbreviations in transcript text.
- **`core/audio_chunker.py`** — `AudioChunker`: split long audio by silence for chunked transcription.
- **`core/audio_converter.py`** — `AudioConverter`: ffmpeg-backed audio conversion and metadata extraction.
- **`core/audio_fingerprint.py`** — `AudioFingerprinter`: content-based audio fingerprint for duplicate detection.
- **`core/audio_quality.py`** — `AudioQualityAnalyzer`: RMS, peak, SNR, clipping ratio, silence ratio analysis.
- **`core/auto_title.py`** — `AutoTitleGenerator`: heuristic auto-title generation from transcript text.
- **`core/confidence_calibrator.py`** — `ConfidenceCalibrator`: calibrate raw Whisper confidence to 0–1 scale.
- **`core/context_memory.py`** — `ContextMemory`: sliding-window context of recent words/topics for STT hints.
- **`core/duplicate_detector.py`** — `DuplicateDetector`: text similarity-based duplicate detection across history.
- **`core/emotion_detector.py`** — `EmotionDetector`: heuristic emotion detection (neutral/positive/negative/etc.).
- **`core/fuzzy_search.py`** — `FuzzySearcher`: approximate string matching for history search.
- **`core/hallucination_manager.py`** — `HallucinationManager`: user-managed custom hallucination patterns.
- **`core/language_detector.py`** — `LanguageDetector`: heuristic script/language detection (RU/ES/EN).
- **`core/model_selector.py`** — `SmartModelSelector`: rule-based STT model selection by duration/load.
- **`core/noise_profiler.py`** — `NoiseProfiler`: background noise type, level, SNR estimation.
- **`core/normalization_profiles.py`** — `NormalizationProfileRegistry`: named text normalization profiles.
- **`core/paste_formatter.py`** — `PasteFormatter`: format transcripts for target apps (Telegram, Notes, Email, etc.).
- **`core/pipeline/`** — Phase 4 deterministic pipeline stages (audio norm, STT, text cleanup, diarization, translation, LLM rewrite, cache).
- **`core/readability_scorer.py`** — `ReadabilityScorer`: Flesch score and sentence/vocabulary complexity.
- **`core/retry_strategy.py`** — `RetryStrategy`: configurable exponential backoff for flaky calls.
- **`core/search_highlighter.py`** — `SearchHighlighter`: highlight query matches in search results.
- **`core/search_index.py`** — `SearchIndex`: in-memory inverted index for fast history search.
- **`core/silence_detector.py`** — `SilenceDetector`: detect silence/speech regions in PCM audio.
- **`core/smart_silence_skipper.py`** — `SmartSilenceSkipper`: skip long silence intervals during recording.
- **`core/speech_pace.py`** — `SpeechPaceAnalyzer`: WPM, CPM, pace category estimation.
- **`core/stop_words.py`** — Stop-word lists for RU/ES/EN used by keyword extraction.
- **`core/text_anonymizer.py`** — `TextAnonymizer`: rule-based PII redaction (phone, email, credit card, etc.).
- **`core/text_diff.py`** — `TextDiff`: word-level diff between two text versions.
- **`core/text_postprocessor.py`** — `TextPostProcessor`: configurable post-processing pipeline (whitespace, punctuation, entities, abbreviations, anonymization).
- **`core/topic_tracker.py`** — `TopicTracker`: track topic shifts across recent transcriptions.
- **`core/transcription_scorer.py`** — `TranscriptionScorer`: composite quality score 0–100 (A–F) from confidence, duration, diarization, LLM flags.
- **`core/vad.py`** — `VoiceActivityDetector`: energy-threshold VAD over audio arrays.
- **`core/waveform_generator.py`** — `WaveformGenerator`: downsample PCM for GUI waveform visualization.
- **`contracts/`** — Pydantic models for event payloads (STT, Translation). `EventType` enum + `EVENT_SCHEMA_MAP` for runtime dispatch. JSON Schema export via `python -m contracts.export`.

### Native agent (`native/KrabEarAgent/`):
- Swift Package (swift-tools-version 6.0, macOS 13+). Single executable target.
- Communicates with backend exclusively through Unix socket JSON-RPC.
- Resolves project root by checking for `KrabEar/backend/service.py`.
- **`KrabEarTheme.swift`** — Liquid Glass visual theme (NSVisualEffectView). ThemeCardView, CollapsibleSectionView, ThemePrimaryButton.
- **`ThemeButton` base class** (PR #13) — общий предок для `ThemePrimaryButton` / `ThemeSecondaryButton`. Устанавливает `NSTrackingArea`, обрабатывает `mouseEntered/Exited/Down/Up` и применяет `KrabEarTheme.Interaction` токены: hover = 10% белый overlay, pressed = 15% чёрный overlay + scale 0.98×, disabled = opacity 40%. Все переходы идут через `KrabEarTheme.Motion.animate()` — Reduce Motion respected.
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

Tests use `unittest.TestCase` with fake/stub collaborators (e.g., `FakeRecorder`, `FakeTranscriber`). Integration tests create temp directories for `StateStore`. No external services required for test suite. Current count: 4482 passed across 178 test files.

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
- **Audio import limits & errors** (PR #12): `MAX_AUDIO_MB` default = 1000 MB (часовые ALAC/AAC звонки 70-100 MB норма); `backend/service.py` ловит русский паттерн "Файл слишком большой" в err_msg matching. Swift `HistoryPanelController+Import.swift` прокидывает actual backend error messages в UI: первые 3 в alert, все в `.md` отчёт под `## Errors` секцией (поле `importErrorMessages: [String]`).
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

## Working guidelines for Claude sessions

### Sub-agent model selection (cost-conscious)

Используй Agent tool с явным `model` параметром — **default opus сжигает quota** (user установил правило 2026-04-17 после 5h quota hit).

| Model | Use for | % of tasks |
|-------|---------|------------|
| `haiku` | Research, docs, diagnostics, simple edits, memory updates, file reads, grep | ~80% |
| `sonnet` | Implementation PRs, Gemini apply, rebase с conflict resolution, tests, medium refactors | ~18% |
| `opus` | Критический debugging (cascading compiler errors), architectural decisions, когда Sonnet уже failed | ~2% |

Параллелизм > глубина: **многих Haiku параллельно** лучше чем одного Opus linear (5-10× throughput при comparable cost).

### Gemini 3.1 Pro для дизайна (strict rule)

Визуальный дизайн (цвета, шрифты, layout, themes, design tokens) делается **ТОЛЬКО** через Gemini 3.1 Pro API:
- Endpoint: `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent?key=AIzaSyCBHw753dZVMQY6wA_08YlVdv2mq8-gtsE`
- Pattern: draft brief `/tmp/krab-ear-gemini/<name>_payload.json` → `curl POST` → save response → apply by sub-agent.
- Claude НЕ делает визуал сам. Граница: "стало выглядеть иначе" → Gemini; "стало себя вести иначе" → Claude/Sonnet.
- Behavior код (Auto Layout mechanics, ThemeButton tracking areas, state machines) — ОК для Claude.

### TCC permissions troubleshooting

macOS TCC (Accessibility, Microphone) кэширует grants по (bundle-id OR absolute path). После rebuild binary с изменённой hash:
- Старые path-based entries в TCC.db остаются но "смотрят" на stale paths.
- Текущий `com.antigravity.krab-ear` bundle ID **может не совпасть** с историей.
- Симптом: user грантит tumбler, app сразу опять запрашивает permission.

**Diagnostic**: `sqlite3 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" "SELECT client, service, auth_value FROM access WHERE client LIKE '%krab%';"`

**Fix workflow** (only do когда user asks):
1. `pkill -9 -f KrabEarAgent`
2. `tccutil reset All com.antigravity.krab-ear`
3. `tccutil reset All com.krabear.agent`  # старый ID
4. `tccutil reset Accessibility <absolute-path-to-old-binaries>` для каждого path-based entry
5. User вручную очищает System Settings → Privacy → Accessibility список (удаляет дубликаты), добавляет ОДИН новый `.app`.
6. Re-toggle ON.

### Parallel PR workflow (session 1 proven pattern)

1 session может merge 11+ PRs с этим подходом:
- **File-level isolation** per sub-agent (each agent owns single file/feature), низкий merge conflict.
- **CI parallelization** (3 min backend-tests каждый PR) не блокирует coordinator work.
- **Merge train**: когда 3+ PRs одновременно конфликтуют — rebase всех параллельно через sub-agents, merge sequentially.
- **Research-first for big decisions**: run 3-4 parallel Haiku research agents BEFORE writing implementation plan (Moshi MLX, SeamlessM4T MLX, qwen3-30b benchmarks — каждый ~5 min, results inform plan).

### Voice Assistant Mode (Phase 1, 2026-04-17 спек)

Большая трансформация в процессе. Spec: `docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md` (330 lines). Plan: `docs/superpowers/plans/2026-04-17-voice-assistant-mode.md` (556 lines, 8 PRs × 3-4 weeks).

Stack:
- **Engines**: Kyutai Moshi 7B (EN) + SeamlessStreaming 2.5B (RU/ES/multilingual, PyTorch+MPS не MLX).
- **Brain**: `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit` via Krab agent OpenClaw.
- **Orchestration**: Voice Gateway `/v1/sessions/{id}/conversation` WS endpoint.
- **UI**: новый tab "Разговор с AI" в Krab Ear `.app` (`ConversationViewController`).
- **Triggers**: GUI button + Right Option double-tap (300ms) + Silero wake word "Краб".
- **Brain stack**: Krab agent (Telegram userbot) — общая memory + MCP tools + OpenClaw. Voice assistant = "новый channel" в same brain.

Phase 2 (Live Translation), Phase 3 (Call Automation), Phase 4 (STT adapters SenseVoice/Parakeet) — отдельные sub-projects, roadmap в specs/.
