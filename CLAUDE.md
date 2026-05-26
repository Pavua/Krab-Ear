# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Krab Ear is a local voice assistant/transcriber for macOS. It runs as a two-process system:
- **Native Swift agent** (`native/KrabEarAgent/`) — handles global hotkey (Right Option), UI panel, accessibility paste, and supervises the Python backend via Unix socket IPC.
- **Python backend** (`KrabEar/`) — performs offline STT via `mlx-whisper`, speaker diarization via `pyannote.audio`, translation, and manages transcription history.

The project is bilingual (RU/ES primary, EN secondary). Code comments, UI labels, and docs are in Russian.

## Architecture

```
┌─────────────────────────┐    Unix socket (JSON-RPC)    ┌──────────────────────────┐
│  Swift Agent (macOS)    │ ◄────────────────────────── ►│  Python Backend           │
│  - HotkeyManager        │                              │  - IPCServer              │
│  - PasteService         │                              │  - BackendService (hub)   │
│  - HistoryPanel         │    Krab Ear.app/             │    → CallAssistSvc        │
│  - BackendSupervisor    │    (bundle wraps agent       │    → HistorySvc           │
│  - KrabEarTheme         │     + Python venv)           │    → TranslationSvc       │
│  - CollapsibleSection   │                              │    → SettingsSvc          │
│  - RealtimeOverlay      │                              │    → RecordingCoreSvc     │
│  - NotificationService  │                              │    → TextProcessingSvc    │
│  - LaunchAgentManager   │                              │    → TextScoringSvc       │
│  - SystemAudioDucking   │                              │    → AnalyticsSvc         │
│                         │                              │    → AudioAnalyticsSvc    │
│                         │                              │    → HealthCheckSvc       │
│                         │                              │    → STTManagementSvc     │
│                         │                              │    → AppleIntegrationSvc  │
│                         │                              │    → CallSessionSvc       │
│                         │                              │    → LiveSubsSvc          │
│                         │                              │  - AudioRecorder          │
│                         │                              │  - Transcriber            │
│                         │                              │  - Translator             │
│                         │                              │  - LLMRewriter            │
│                         │                              │  - StateStore (NDJSON)    │
│                         │                              │  - MetricsCollector       │
│                         │                              │  - VGWSClient             │
└─────────────────────────┘                              └──────────────────────────┘
```

### Service map (post-W751)

14 services extracted from `BackendService` — zero orphan imports as of W751 (guarded by CI):

1. **CallAssistService** — call assist + VG WS client
2. **HistoryService** — history CRUD, SRT export, clipboard hist
3. **TranslationService** — translate, glossary mgmt
4. **SettingsService** — settings CRUD + profile presets + 5s TTL cache
5. **RecordingCoreService** — start/stop_recording + transcribe_paths
6. **TextProcessingService** — score readability/transcription, abbrev, post-process
7. **TextScoringService** — warmup_rewriter, extract_terms, auto_title
8. **AnalyticsService** — dashboard, sentiment trends, period compare, keyword cloud
9. **AudioAnalyticsService** — audio quality, waveform, trends
10. **HealthCheckService** — ping (contract bit-exact), diagnostics, integrity check
11. **STTManagementService** — STT hotwords CRUD, warmup, routing
12. **AppleIntegrationService** — Telegram bridge, Notes, Reminders, Calendar, iMessage
13. **CallSessionService** — call session CRUD + status lifecycle
14. **LiveSubsService** — system-audio streaming STT for live subtitles (Phase 2)

Note: `TTSService` (`backend/tts_service.py`) is standalone — not extracted from `service.py`.

Orphan-import regression guard: `scripts/audit_orphan_imports.py` runs in CI on every push.

### Key layers inside `KrabEar/`:
- **`core/config.py`** — Pydantic-Settings singleton (`settings`), all params overridable via `KRAB_EAR_*` env vars. Also contains `DEFAULT_SETTINGS` dict used by UI/IPC.
- **`core/engine.py`** — `AudioEngine`: STT via mlx-whisper with fallback chain (balanced → max candidates → remote), audio normalization, diarization pipeline (pyannote), TTS via macOS `say`.
- **`core/utils.py`** — `TextUtils`: transcript cleanup (soft/strict profiles), hallucination stripping, phrase dedup.
- **`backend/service.py`** — `BackendService` (business logic) + `IPCServer` (Unix socket server). Single file, **~3224 lines** (down from 5821 baseline, −45% after marathon extractions). The `handle_request` method dispatches **300** JSON-RPC methods via a handler lookup table, delegating to **14 extracted services** with **zero remaining orphans** (HealthCheckService wired in W751 closed the last gap). Wave 65 batch 1 removed 19 dead handlers; marathon waves W172/W392/W404/W423/W525/W683/W691/W734/W741/W742/W746/W747/W751 added RecordingCoreService, AnalyticsService, TextScoringService, HealthCheckService, AppleIntegrationService, STTManagementService, TextProcessingService and others. **W746 lesson**: the `TextProcessingService` import was silently lost in a W173 rebase — instantiation survived but import vanished — production stayed alive only because Python had loaded the .py before the drop. **W750 audit script** (`scripts/audit_orphan_imports.py`) is wired into CI to catch this regression class. Post-W742 the lookup table may bypass local `_handle_X` shims and call `self._<svc>.handle_X` directly for already-extracted handlers. Full API reference: `docs/IPC_API_REFERENCE.md` (regen in W745, ~2422 lines, 289 handlers documented) — cross-check live via `grep -cE '"[a-z_]+":\s*self\._' KrabEar/backend/service.py`.
- **`backend/call_assist_service.py`** — `CallAssistService`: call assist delegation, VoiceGatewayClient integration.
- **`backend/history_service.py`** — `HistoryService`: history CRUD, SRT export, clipboard history, storage info.
- **`backend/translation_service.py`** — `TranslationService`: translate, glossary management, vocabulary suggestions.
- **`backend/settings_service.py`** — `SettingsService`: settings CRUD, profile presets, 5s TTL cache.
- **`backend/recorder.py`** — `AudioRecorder`: thread-safe start/stop audio capture via `sounddevice`.
- **`backend/state_store.py`** — `StateStore`: append-only NDJSON history with tombstone deletes, file-lock, and compaction. Settings stored as settings.json (runtime data file, not in repo).
- **`backend/transcriber.py`** — Thin wrapper over `AudioEngine` for profile/vocabulary management.
- **`backend/translator.py`** — Offline-first translator (RU↔ES, EN→RU, Auto, Bilingual modes) with in-memory cache.
- **`backend/llm_rewriter.py`** — LLM post-processing via LM Studio (qwen3-4b-abliterated). CircuitBreaker + chatbot detection + length ratio guard.
- **`backend/rest_server.py`** — Flask REST API (port 5005) for HTTP-based transcription and metrics. Separate from the IPC service.
- **`backend/event_bus.py`** — In-process pub/sub EventBus with SSE streaming. Supports both untyped `emit(str, dict)` and typed `emit_typed(EventType, BaseModel)`.
- **`backend/metrics_collector.py`** — Thread-safe sliding-window metrics (latency percentiles, confidence).
- **`backend/obsidian_sync.py`** — `ObsidianSyncManager`: sync transcriptions to an Obsidian vault as .md files with YAML frontmatter; incremental (timestamp-based) and forced modes; state persisted in obsidian_sync.json (runtime data file, not in repo).
- **`backend/sentiment_trends.py`** — `SentimentTrendAnalyzer`: daily sentiment aggregation over history items using `EmotionDetector`; linear-regression mood trend (`improving`/`stable`/`declining`).
- **`backend/collection_manager.py`** — `CollectionManager`: named collections of history items; CRUD + bulk operations.
- **`backend/daily_digest.py`** — `DailyDigestGenerator`: daily summary digest of transcription activity.
- **`backend/integrity_checker.py`** — `IntegrityChecker`: NDJSON integrity validation and repair for history store.
- **`backend/period_comparison.py`** — `PeriodComparisonService`: compare transcription statistics across arbitrary time periods.
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
- **`backend/html_report.py`** — `HTMLReportGenerator`: standalone HTML analytics report.
- **`backend/input_sanitizer.py`** — `InputSanitizer`: validate and sanitize IPC params.
- **`backend/ipc_throttle.py`** — `IPCThrottle`: per-method rate limiting (token bucket) for heavy IPC calls.
- **`backend/keyword_cloud.py`** — `KeywordCloudGenerator`: word-cloud data (count, weight, font_size) from history.
- **`backend/language_learning.py`** — `LanguageLearningManager`: bilingual vocabulary extraction and flashcard generation.
- **`backend/model_cache_manager.py`** — `ModelCacheManager`: HuggingFace model cache management.
- **`backend/performance_profiler.py`** — `PerformanceProfiler`: elapsed-time profiling for backend operations.
- **`backend/period_comparison.py`** — `PeriodComparisonService`: compare transcription statistics across arbitrary time periods. *(listed above)*
- **`backend/playback_tracker.py`** — `PlaybackTracker`: persistent playback event tracking (play count, total listened).
- **`backend/plugin_system.py`** — `PluginManager`: simple plugin loader for extensibility.
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
- **`contracts/`** — Pydantic models for event payloads (STT, Translation, LiveSubs). `EventType` enum + `EVENT_SCHEMA_MAP` for runtime dispatch. JSON Schema export via `python -m contracts.export`.

#### Phase 2 — Live Translation modules:
- **`backend/live_subs_service.py`** — `LiveSubsService`: streaming STT + translate for system audio subtitles. Accumulates base64 PCM 16 kHz chunks, flushes every ≥3 s or on `is_final=True`, emits `live_subs.result` via EventBus.
- **`backend/glossary_auto_learn.py`** — `GlossaryAutoLearn`: auto-extract domain terms (medical etc.) from recent translation history and propose glossary entries.

#### Phase 3 — Call Automation modules:
- **`backend/call_session.py`** — `CallSession` data model + `CallStatus` state machine (`idle→dialing→connected→talking→ending→completed/failed`).
- **`backend/call_session_store.py`** — `CallSessionStore`: persistent storage of call sessions (NDJSON).
- **`backend/call_cost_estimator.py`** — `CallCostEstimator`: compute per-minute telephony cost estimate before dialing; shows ticker during active call.
- **`backend/call_silence_probe.py`** — `CallSilenceProbe`: detect >10 s silence during a call to trigger soft end.
- **`backend/call_auto_end.py`** — `CallAutoEnd`: enforce max-duration limit (default 30 min) and silence-based auto-hangup.
- **`backend/telnyx_adapter.py`** — `TelnyxAdapter`: Telnyx Call Control REST API adapter for outbound calls; Bearer-auth + exponential-retry; stub-mode when `TELNYX_API_KEY` absent.
- **`backend/observability.py`** — `init_sentry()` / `capture_exception()` helpers; Sentry/GlitchTip SDK init; fully no-op when DSN not provided.
- **`backend/telegram_bridge.py`** — `TelegramBridge`: send messages from Krab Ear backend to main Krab userbot via `POST /api/notify` on localhost web-panel port.
- **`backend/openwakeword_adapter.py`** — `OpenWakeWordAdapter`: Apache-2.0 wake-word detection (openWakeWord); primary engine until Picovoice (no email/signup); custom "Краб" model requires ~15 min Jupyter training.

#### Twilio / provider abstraction (Phase 3 step 5):
- **`backend/twilio_adapter.py`** — `TwilioAdapter`: Twilio REST API adapter, same interface as `TelnyxAdapter`. Active provider selected via `CALL_PROVIDER` setting (`telnyx` | `twilio`); swap at runtime without code changes.
- **`backend/call_provider.py`** — `CallProvider`: Protocol (structural typing) defining the common interface all telephony adapters must implement.
- **`backend/call_provider_factory.py`** — `get_provider()`: returns the active `CallProvider` adapter instance based on `CALL_PROVIDER` setting.

#### Additional backend modules:
- **`backend/action_items_extractor.py`** — `ActionItemsExtractor`: extract tasks, decisions, and questions from meeting transcripts with priority tagging.
- **`backend/activity_calendar.py`** — `ActivityCalendar`: GitHub-style contribution graph data (recordings per day) for the history UI.
- **`backend/api_versioning.py`** — `APIVersion`: enum + deprecation metadata for REST API v1/v2 version negotiation.
- **`backend/archive_manager.py`** — `ArchiveManager`: move old history entries into a separate `archive.ndjson` file to keep the main store lean.
- **`backend/auto_deduplication.py`** — `AutoDeduplicator`: automatically skip or merge near-duplicate history items above a configurable similarity threshold.
- **`backend/bookmarks.py`** — `BookmarkManager`: timestamped bookmarks on long recordings; stored as NDJSON tombstones for deletion.
- **`backend/bulk_reprocess.py`** — `BulkReprocessor`: batch re-transcribe old history entries using the current STT settings.
- **`backend/calendar_link.py`** — `CalendarLinker`: link transcriptions to overlapping Calendar.app events via osascript.
- **`backend/default_hotwords.py`** — curated list of default STT hotwords (AI names, tech terms, RU/ES proper nouns) loaded at startup.
- **`backend/disk_monitor.py`** — `DiskSpaceMonitor`: background thread that warns when data-dir free space falls below 2 GB threshold.
- **`backend/email_sender.py`** — `EmailSender`: send transcription digests via SMTP or macOS Mail.app with Keychain password retrieval.
- **`backend/health_checker.py`** — `HealthChecker`: aggregate readiness checks for all backend subsystems (disk, IPC socket, STT model) into a single status dict.
- **`backend/ipc_constants.py`** — module-level IPC socket constants (backlog, timeout, max message bytes) shared across service and supervisor.
- **`backend/job_tracker.py`** — `JobTracker`: thread-safe in-memory store for async transcription job states (queued/running/done/failed/cancelled).
- **`backend/lm_studio_lifecycle.py`** — `load_model_async()` / `unload_model_async()`: load and unload LM Studio models via REST API with CLI fallback for memory management.
- **`backend/metadata_enricher.py`** — `MetadataEnricher`: auto-populate language, sentence count, word count, and keywords fields on history items.
- **`backend/models.py`** — `HistoryItem` and related Pydantic dataclasses (shared data models used across backend services).
- **`backend/paste_app_memory.py`** — `PasteAppMemory`: remember per-application paste format preferences between sessions.
- **`backend/privacy_audit.py`** — `PrivacyAuditLogger`: singleton NDJSON log of privacy-mode events (enable/disable/purge) for compliance auditing.
- **`backend/realtime_partial.py`** — `RealtimePartialTranscriber`: background thread that emits partial (`realtime.partial_transcript`) and final transcript events via EventBus during active recording.
- **`backend/realtime_silence_filter.py`** — `RealtimeSilenceFilter`: energy-based silence detector that suppresses partial events during long pauses in real-time recording.
- **`backend/recap_scheduler.py`** — `RecapScheduler`: daily cron-like scheduler that generates and emails a transcription digest at a configured hour.
- **`backend/rest_auth.py`** — `RestAuth`: Bearer-token store (hashed) for optional authentication on the REST server (port 5005).
- **`backend/search_history.py`** — `SearchHistoryManager`: persist and recall recent IPC search queries for autocomplete.
- **`backend/semantic_search.py`** — `SemanticSearcher`: sentence-embedding index (`multilingual-e5-base`) for semantic similarity search over transcription history.
- **`backend/session_tracker.py`** — `SessionTracker`: per-recording session metadata (start/end, device, mode) written alongside history items.
- **`backend/settings_backup.py`** — `SettingsBackup`: rolling backup of settings.json before each write, with sensitive-field redaction.
- **`backend/settings_validator.py`** — `SettingsValidator`: validate settings dict against allowed enum values and migrate older schema versions to `2.0`.
- **`backend/shutdown_handler.py`** — `GracefulShutdownHandler`: coordinate orderly backend shutdown (flush stores, cancel jobs, write a runtime shutdown info file).
- **`backend/stats_report.py`** — `StatsReportGenerator`: generate a comprehensive Markdown statistics report (top words, durations, language breakdown) from history.
- **`backend/timeline_export.py`** — `TimelineExporter`: export recording timeline as SVG, JSON, or iCalendar (`.ics`) file.
- **`backend/transcript_writer.py`** — `TranscriptWriter`: write each transcription to a timestamped Obsidian-compatible Markdown file in the transcripts directory.
- **`backend/tts_service.py`** — `TTSService`: dual-engine TTS (Silero RU primary, Kokoro EN fallback, macOS `say` last resort) with language auto-detection.
- **`backend/vg_ws_client.py`** — `VGWebSocketClient`: WebSocket client to the Voice Gateway `/v1/sessions/{id}/stream` endpoint for real-time conversation streaming.

#### Additional core modules:
- **`core/audio_denoiser.py`** — `AudioDenoiser`: adaptive spectral-gating noise reduction with configurable strength levels (`off/light/moderate/strong`) before STT.
- **`core/audio_lang_id.py`** — `AudioLanguageID`: language identification from raw audio via mlx-whisper encoder (encoder-only, no decode) returning ISO 639-1 codes.
- **`core/auto_glossary.py`** — `AutoGlossary`: build and cache a domain-specific glossary from transcription history for STT initial-prompt injection.
- **`core/code_switching_detector.py`** — `CodeSwitchingDetector`: detect mid-sentence language switches (RU↔ES↔EN) in transcribed text, excluding technical tokens.
- **`core/datetime_normalizer.py`** — `DateTimeNormalizer`: normalize spoken date/time expressions (Russian inflected forms, Spanish, numeric) to ISO-8601 in transcripts.
- **`core/gain_normalizer.py`** — `GainNormalizer`: RMS-based audio gain normalization to a target dB level before STT pipeline.
- **`core/mlx_inter_lock.py`** — `mlx_inter_process_lock()`: POSIX `flock`-based cross-process serialization of MLX GPU access (wraps `mlx_lock` for multi-process safety).
- **`core/mlx_lock.py`** — `mlx_lock()`: global intra-process `RLock` for serializing all MLX/mlx-whisper inference calls to prevent SIGSEGV on concurrent GPU access.
- **`core/mlx_subprocess.py`** — MLX inference watchdog: runs MLX transcription in a subprocess with a configurable timeout and auto-recovery on GPU hang.
- **`core/number_normalizer.py`** — `NumberNormalizer`: expand spoken Russian and Spanish cardinal/ordinal numerals to digit form in transcripts.
- **`core/parsing_utils.py`** — shared JSON parsing helpers (`safe_json_loads`) with graceful fallback and context-aware error logging.
- **`core/stt_router.py`** — `STTRouter`: language-aware routing of audio to the best STT adapter (scored selection or legacy order) with graceful fallback.
- **`core/transcript_context.py`** — `build_initial_prompt()`: builds Whisper `initial_prompt` from recent history items and merged hotword/glossary vocabulary within a 30-minute window.
- **`core/voice_commands.py`** — `VoiceCommandProcessor`: post-STT layer that recognises dictation commands (punctuation, capitalize, delete-last) and applies them to transcript text.
- **`core/word_timing.py`** — `WordTimingAnalyzer`: analyse per-word timestamps from Whisper segments to detect hesitations, pauses, and speech rhythm.

### Native agent (`native/KrabEarAgent/`):
- Swift Package (swift-tools-version 6.0, macOS 13+). Single executable target.
- Communicates with backend exclusively through Unix socket JSON-RPC.
- Resolves project root by checking for `KrabEar/backend/service.py`.
- **`KrabEarTheme.swift`** — Liquid Glass visual theme (NSVisualEffectView). ThemeCardView, CollapsibleSectionView, ThemePrimaryButton.
- **`ThemeButton` base class** (PR #13) — общий предок для `ThemePrimaryButton` / `ThemeSecondaryButton`. Устанавливает `NSTrackingArea`, обрабатывает `mouseEntered/Exited/Down/Up` и применяет `KrabEarTheme.Interaction` токены: hover = 10% белый overlay, pressed = 15% чёрный overlay + scale 0.98×, disabled = opacity 40%. Все переходы идут через `KrabEarTheme.Motion.animate()` — Reduce Motion respected.
- **`HistoryPanelController.swift`** + 12 extension files: `+CallAssist`, `+CallAutomation`, `+Diagnostics`, `+GlossarySuggestions`, `+History`, `+HistoryEnhancements`, `+Import`, `+LiveSubsSettings`, `+LiveTranslation`, `+Management`, `+SelectionTranslator`, `+Settings` (split for maintainability).
- **`RealtimeOverlayController.swift`** — floating overlay for live transcription feedback.
- **`NotificationService.swift`** — macOS user notifications (confidence warnings, errors).
- **`LaunchAgentManager.swift`** — install/remove launchd plist for auto-start.
- **`SystemAudioDuckingService.swift`** — lower system volume during recording.
- **`PermissionWizard.swift`** — guided Accessibility + Microphone permission setup.

#### Phase 2 Swift additions:
- **`SelectionTranslator.swift`** — Cmd+Shift+T global hotkey; reads selected text via AX API (`kAXSelectedTextAttribute`) or clipboard fallback; calls `translate_selection` IPC; writes result back via AX or Cmd+V.
- **`SystemAudioCapture.swift`** — ScreenCaptureKit-based system audio tap; streams base64 PCM 16 kHz to backend IPC `live_subs_ingest`; requires Screen Recording permission.
- **`LiveSubtitlesOverlay.swift`** — floating NSPanel HUD (always on top, draggable) for live subtitles; shows last 3 lines with 4 s auto-fade; subscribes to SSE `live_subs.result` events.
- **`main+LiveSubs.swift`** — wires `SystemAudioCapture` start/stop to menu item and `HistoryPanelController+LiveSubsSettings`.

#### Phase 3 Swift additions:
- **`CallAutomationController.swift`** — manages outbound call lifecycle; integrates with `call_session_*` IPC methods; drives cost ticker, silence probe, auto-end UI.
- **`SentryConfig.swift`** — no-op Sentry/GlitchTip initialisation; reads DSN from `settings.sentry_dsn` via IPC; fully skips SDK init when DSN absent.
- **`SingleInstanceGuard.swift`** — kills duplicate `KrabEarAgent` processes on launch; prevents double-paste and double-hotkey issues.
- **`WakeWordListener.swift`** — openWakeWord adapter bridge (Swift↔Python); triggers recording on wake-word detection; hotkey remains primary fallback.
- **`HotkeyDoubleTapDetector.swift`** — detects Right Option double-tap (300 ms window) to start Voice Assistant conversation.

#### Phase A — Auto-heal (2026-05-02) Swift additions:
- **`BackendSupervisor.swift`** — двухкольцевой supervisor; passive mode (launchd Variant B = `KeepAlive=true`) или active mode (standalone). Exp backoff restart 0/2/5/15s + circuit breaker (5 fails в 60s window → 5 min cooldown). Spec: `docs/superpowers/specs/2026-05-04-phase-c-roadmap-refinement-design.md` Phase A.
  - **Wave 50 self-recovery bug** (FIXED PR #408): `pgrep + set -e` в launchd plist никогда не работал — `set -e` вызывал немедленный exit при non-zero pgrep exit code. Исправлено в `scripts/install_agent_launchagent.command` (убран `set -e`, добавлен explicit check).
- **`HealthMonitor.swift`** — actor с 3s ping `handle_ping` IPC; 2 fails подряд → SIGTERM Python backend → wait → SIGKILL → respawn. Phase B.1 расширит подпиской на `rewriter_recovered` события из active LLM probe.
- **`BackendToast.swift`** — non-modal toast (severity-aware), используется для backend restart notifications в Phase A. Phase B.1 добавит `ErrorToastView` для UI ошибок (отдельный компонент).
  - **AGENT-K fix (PR #406)**: BackendToast crash при ColorSync callback на stale bundle (v2.0.2). Исправлено — guard на nil window + weak capture в colorAppearanceDidChange.
  - **AGENT-M fix (Wave 266)**: BackendToast AppHang при первом показе Cyrillic/emoji сообщения — CoreText glyph-metrics build на main thread. Исправлено — `prewarmPanel()` + правильный порядок `positionPanel()` → `sizeToFit()` → `orderFront()`. Sister регрессия к AGENT-K.
- **`StatusIndicatorView.swift`** — menu bar dot + history panel header dot. Phase A: green/yellow/red по supervisor state. Phase B.1 добавит layered foreground severity badge поверх (info/warn/error/critical).

#### Phase B — Loud Errors (2026-05-04+) Python additions:
- **`backend/error_bus.py`** — `KrabError` Pydantic model + `ErrorBus` (push/dedupe/ring buffer/Sentry tier routing) + `WarnBatcher`. **47** codes wired runtime.
- **`backend/error_codes.py`** — `ERROR_REGISTRY` dict (**47** codes covering paste, rewriter, stt, diarization, translation, mlx, history, vocabulary, hotkey, ipc, disk, audio, system, vgw categories). Wave 60 +5, Wave 61 +3, Wave 64 +5, Wave 78 +7 added codes post-Phase B initial 24.
- **`backend/error_actions.py`** — `ACTION_HANDLERS` dispatch + 8 action handlers (open_privacy_settings, disable_rewriter, etc.).
- **`backend/llm_probe.py`** — `LLMHttpProbe` passive GET `/v1/models` health check (post-PR #364 F2 — was POST /v1/chat/completions which caused JIT churn).

#### Phase B — Loud Errors Swift additions:
- **`ErrorActionHandler.swift`** — KrabErrorPayload Codable + AnyCodable + ToastPresenting protocol + ErrorActionHandler class (handleErrorEvent + handleActionTap + side-effect dispatch).
- **`ErrorToastView.swift`** — `ErrorToastPresenter` Liquid Glass NSPanel (severity-aware auto-dismiss: info=2s/warn=5s/error=10s/critical=manual). Queue. `ToastPanelFactory` protocol for test isolation.
- **`main+Errors.swift`** — `setupErrorBus(toastPresenter:)` extension wires SSE error stream.

#### Phase C — Root Cause (2026-05-04+) additions:
- **`docs/audit/`** — codebase audits (mlx-call-sites, distributed-notifications, gigaam-worker-memory).
- **`docs/measurements/`** — memory baseline CSVs (workflow in README).
- **`scripts/memory_baseline.py`** — psutil-based RSS snapshot to CSV.
- **`scripts/cleanup_worktree_shadows.command`** — drift prevention (lsregister -u worktree-shadow .app).
- **`scripts/verify_claude_md.py`** — CLAUDE.md drift checker (CI integrated).
- **`scripts/profile_gigaam_worker.command`** — opt-in memory profiling driver.

#### Phase B/C IPC methods (additive):
- `list_recent_errors`, `clear_recent_errors`, `handle_error_action`, `probe_llm_http`,
- `report_paste_failure`, `report_hotkey_conflict`, `report_reconnect`,
- `list_llm_models`, `handshake`.

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
~/Antigravity_AGENTS/new\ start_krab.command   # СТАРТ  (абсолютный путь: /Users/<you>/Antigravity_AGENTS/)
~/Antigravity_AGENTS/new\ Stop\ Krab.command    # СТОП
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

Tests use `unittest.TestCase` with fake/stub collaborators (e.g., `FakeRecorder`, `FakeTranscriber`). Integration tests create temp directories for `StateStore`. No external services required for test suite. Current count: **~6500+ passed** across 246+ test files.

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
- `scripts/repair_permissions.command` — reset TCC + re-grant Accessibility/Microphone (PR #234)
- `scripts/create_local_signing_identity.command` — create `Krab Ear Dev Local` self-signed identity for stable TCC grants (PR #235)
- `scripts/build_distribution_dmg.command` — build distribution DMG for sharing (PR #229)
- `scripts/install_agent_launchagent.command` — opt-in launchd KeepAlive for Swift agent (Wave 59 self-recovery)

### Launch app
```bash
# Open the .app bundle (production, launchd-managed backend)
open "Krab Ear.app"

# Or run agent binary directly (dev mode, manual backend)
python KrabEar/main.py --data-dir ~/.krab_ear_data &
./native/runtime/KrabEarAgent
```

### Sentry / observability
```bash
# Enable Sentry crash reporting (set DSN via IPC)
# Socket path: production = default_data_dir()/krabear.sock (see KrabEar/core/config.py)
#              dev        = ~/.krab_ear_data/backend.sock (via --data-dir flag)
python3 -c "
import os, socket, json
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
# Production socket (launchd Variant B):
sock.connect(os.path.expanduser('~/Library/Application Support/KrabEar/krabear.sock'))
sock.sendall(json.dumps({'id':'1','method':'set_settings','params':{'sentry_dsn':'https://YOUR_DSN@sentry.io/PROJECT_ID'}}).encode()+b'\n')
print(sock.recv(4096).decode())
"

# Sentry alerts also land in: ~/Library/Logs/KrabEar/sentry_errors.log (when log level=debug)
# (log dir = default_data_dir()/logs/ — matches data dir, not hardcoded)
```

### Performance benchmarks (PR #237 / #242)
```bash
# Run benchmark suite and store baseline snapshot
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_performance_benchmarks.py -v

# View regression history (stored in .perf_baselines/):
python scripts/check_performance_budget.py

# Regression gate runs automatically in CI; fails if p95 latency regresses >15%
```

### Call automation (Phase 3 — Telnyx / Twilio)
```bash
# Set provider credentials via IPC (one-time)
# Telnyx: sign up at telnyx.com → Mission Control → API Keys
# Twilio:  sign up at twilio.com → Console → Account SID + Auth Token

# Switch active provider (default: telnyx)
# set_settings { "call_provider": "telnyx" }   # or "twilio"
# set_settings { "telnyx_api_key": "KEY_...", "telnyx_phone": "+1..." }
# set_settings { "twilio_account_sid": "AC...", "twilio_auth_token": "...", "twilio_phone": "+1..." }
```

## Important Patterns

- **IPC protocol**: JSON-RPC-like over Unix socket. Path depends on how backend was launched:
  - **Production (launchd Variant B, see `scripts/install_backend_launchagent.command`)**: `~/Library/Application Support/KrabEar/krabear.sock` (default = `default_data_dir()` in `core/config.py`)
  - **Dev standalone** (`python KrabEar/main.py --data-dir ~/.krab_ear_data`): `~/.krab_ear_data/backend.sock`
  
  Request format: `{"id": "...", "method": "...", "params": {...}}`. Response: `{"id": "...", "ok": true, "result": {...}}`.
- **History storage**: Append-only NDJSON (`history.ndjson`) with tombstone-based deletes and periodic compaction. All writes are file-lock protected.
- **STT fallback chain**: balanced model → max model candidates → remote STT (if network mode allows). Unavailable models are tracked in `_unavailable_models` set.
- **LLM post-processing**: engine.py hooks into LLMRewriter after STT, before paste. Chatbot guard rejects responses starting with known assistant phrases. Length ratio guard rejects output <35% or >300% of input.
- **Collapsible GUI sections**: CollapsibleSectionView with UserDefaults persistence (key: `CollapsibleSection_{sectionId}`). Disclosure triangle toggle with animation.
- **iCloud audio import**: files from `Mobile Documents/com~apple~CloudDocs` are auto-copied to /tmp before ffmpeg (errno 11 workaround).
- **Audio import limits & errors** (PR #12): `MAX_AUDIO_MB` default = 1000 MB (часовые ALAC/AAC звонки 70-100 MB норма); `backend/service.py` ловит русский паттерн "Файл слишком большой" в err_msg matching. Swift `HistoryPanelController+Import.swift` прокидывает actual backend error messages в UI: первые 3 в alert, все в `.md` отчёт под `## Errors` секцией (поле `importErrorMessages: [String]`).
- **Transcript files**: imported audio generates .md files in `<data_dir>/transcripts/` (production default: `~/Library/Application Support/KrabEar/transcripts/`; dev: `~/.krab_ear_data/transcripts/`). Data dir resolved via `default_data_dir()` in `KrabEar/core/config.py`.
- **Legacy compatibility**: `AudioEngine` has static method aliases (`_cleanup_soft`, `_normalize_phrase`, etc.) that delegate to `TextUtils` — these exist for backwards compatibility with older tests.
- **Config override**: Any setting in `core/config.py` can be overridden via `KRAB_EAR_<SETTING_NAME>` environment variable.
- **Runtime vs static settings reads (Wave 58 lesson)**: ВСЕ startup-time reads of user-overridable settings MUST use `self._get_runtime_setting(key, default)` (lines 593-601 в `service.py`), NOT `DEFAULT_SETTINGS.get(key, default)`. The latter reads the static dict imported at module load, ignoring settings.json runtime overrides — caused chronic warmup-timeout warnings (Wave 58 fix: rewriter_warmup line 187 + stt_warmup line 229). Legit fallback usages of `DEFAULT_SETTINGS` are nested: `cached_settings.get(key, DEFAULT_SETTINGS.get(key, hardcoded))` — runtime first, static as ultimate fallback.
- **Test path setup**: Test files manually prepend `PROJECT_ROOT` to `sys.path` to resolve `backend.*` and `core.*` imports when run standalone.
- **Event contracts**: All events use `{type, ts, data}` envelope (EVENT_CONTRACT_V1). Event types are defined in `contracts/registry.py`. Each service owns its event schemas — Krab Ear owns STT + Translation, Voice Gateway owns TTS + Session.
- **Release process**: `RELEASE_CHECKLIST.md` at repo root. Automated part via `scripts/run_release_checklist.command`.
- **Service extraction pattern**: each extracted service takes `store` + specific collaborators in its constructor; handler methods named `handle_*`; `BackendService` imports the service and delegates matching IPC methods to it.
- **Dead code removal workflow**: extract logic into new service → add delegation calls in `BackendService.handle_request` → verify all tests pass → remove original methods from `BackendService`. Wave 65 batch 1 shipped (PR #410, 19 removed → 306 active); subsequent batches reduced count to **296 active** (post v2.0.3). **Critical audit lesson**: dead handler candidates MUST be checked in Python test scope too (grep `assert_dispatch`, `_handle_X` direct calls, `handle_request` calls) — naive Swift-only grep over-counts by ~4×. Reference: Wave 65 audit found 177 candidates but only 19 confirmed dead after full test scope check.
- **CallAssistService delegation**: `HistoryPanelController+CallAssist.swift` delegates all call assist logic to `CallAssistService` (Python backend); Swift side is thin UI/IPC glue only.
- **JSON structured logging**: `LOG_FORMAT` setting (`json` or `text`).
  - **When using `json`**: handlers use `JsonFormatter` defined inline in `backend/service.py::configure_logging` (`service.py:6168-6186`). REST API server (`backend/rest_server.py:280`) emits its own structured records inline via `json.dumps(log_record)` — same `ts/level/...` shape.
  - **Preferred logging pattern**: `logger.info("message", extra={"key": value, ...})` — structured context required for new code. Any non-standard `LogRecord` attribute (i.e. all keys in `extra={...}`) is merged into the JSON output. Standard attrs are filtered via `_STANDARD_LOG_ATTRS` frozenset in `configure_logging`.
  - **Don't use `print()`** in production code. Exceptions: doctest examples, CLI scripts.
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
- **Sentry observability**: `backend/observability.py` wraps `sentry_sdk`. Call `init_sentry(dsn)` at startup (done in `main.py`). All functions are no-op when `dsn` is `None` or empty — safe to ship without a DSN. Swift side: `SentryConfig.init(dsn:)` reads `settings.sentry_dsn` via IPC on launch. Self-hosted GlitchTip (Sentry-compatible) is fully supported.
- **Call provider abstraction**: `CALL_PROVIDER` setting (`telnyx` | `twilio`) selects the active adapter at runtime. Both adapters (`TelnyxAdapter`, `TwilioAdapter`) implement the same interface. Credentials are per-provider settings: `telnyx_api_key` / `twilio_account_sid` + `twilio_auth_token`. Stub mode active when credentials absent.
- **Single-instance guard**: `SingleInstanceGuard.swift` runs at app startup and kills any existing `KrabEarAgent` process (same bundle path). Prevents double-paste, double-hotkey, and IPC port conflicts after crash-restart.
- **Selection translate flow (Phase 2A)**: Cmd+Shift+T → `SelectionTranslator.swift` → (A) AX API reads `kAXSelectedTextAttribute` → sends `translate_selection` IPC → writes back via `AXUIElementSetAttributeValue`; (B) fallback: save clipboard → Cmd+C → read clipboard → translate → Cmd+V → restore clipboard. Failure shows error HUD, never mutates text.
- **Live subtitles flow (Phase 2B)**: `SystemAudioCapture.swift` (ScreenCaptureKit) taps system audio → base64-encodes 16 kHz PCM chunks → `live_subs_ingest` IPC → `live_subs_service.py` accumulates ≥3 s → Whisper STT → translate → emits `live_subs.result` via EventBus → SSE stream → `LiveSubtitlesOverlay.swift` HUD panel. Requires Screen Recording permission.
- **MLX thread-safety**: MLX (mlx_whisper, mlx.core) is NOT thread-safe — concurrent GPU access corrupts internal `__hash_table<MTL::Resource*>` causing SIGSEGV. ALL MLX inference must be serialized through `core.mlx_lock.mlx_lock()` (RLock — reentrant). Pattern:
  ```python
  from core.mlx_lock import mlx_lock
  
  with mlx_lock():
      result = mlx_whisper.transcribe(audio, ...)
  ```
  - PyTorch+MPS adapters (SenseVoice, Parakeet, WhisperX, Voxtral) don't need this lock.
  - Profile switches (balanced↔max) trigger model reload in MLX → protect these too.
  - 2026-04-19 crash report: `~/Library/Logs/DiagnosticReports/Python-2026-04-19-213636.ips`. Fix: PR #71.
  - **Wave 63 memory leak fix (PR #405)**: call `mx.clear_cache()` after each `mlx_whisper.transcribe()` + bound `audio_lang_id` model cache — prevents RAM growth on long sessions. Production evidence: backend RSS stable at 35–40 MB vs 408 MB pre-fix growth on extended sessions.

- **Sentry breadcrumbs (PR #238)**: `backend/observability.py` logs privacy-respecting breadcrumbs (no transcript text, only metadata: method name, duration_ms, error_type). Breadcrumbs auto-attach to next crash report. Pattern: `add_breadcrumb(category="ipc", message="method_name", data={"ok": True})`.
- **Sentry release tracking (PR #241)**: `SentryConfig.swift` reads `CFBundleVersion` and sets `sentry_sdk.set_tag("release", version)` at startup. Enables regression tracking per release in Sentry issues dashboard. Python side sets `release=` in `sentry_sdk.init()`.
- **Stable codesign identity (PR #235)**: `scripts/create_local_signing_identity.command` creates a self-signed cert `Krab Ear Dev Local` in the system keychain. Sign binary with: `codesign -s "Krab Ear Dev Local" -f ...`. TCC grants persist across rebuilds because the identity hash stays constant. **Caveat**: for distribution (App Store / Notarization), replace with Apple Developer ID. See `docs/DEV_CODESIGN.md`.
- **Distribution DMG (PR #229)**: `scripts/build_distribution_dmg.command` creates a signed `.dmg` for sharing. Requires `Krab Ear Dev Local` identity or Apple Developer ID. See `docs/DISTRIBUTION.md`.
- **Analytics UI (PR #231 / #233)**: `AnalyticsDashboardViewController.swift` renders the analytics dashboard via `get_analytics_dashboard` IPC. Shows sentiment trend, quality trend, keyword cloud. Bug fixes in PR #233 (nil guard crash on empty history).
- **IPC full reference**: `docs/IPC_API_REFERENCE.md` — 4341 lines, JSON-RPC handlers documented with params/response schema and examples (PR #243). Active handler count **296** (Wave 65 batch 1 removed 19 dead from 325; subsequent batches brought to 296 as of v2.0.3 — see dead code removal workflow note). Use as ground truth before implementing new IPC calls.
- **Wave 67 (PR #412)**: `StatusIndicatorView.swift` — replaced `●` Unicode literal with SF Symbol `circle.fill` to fix font hang (AGENT-J root cause was CoreText attempting to render Unicode bullet in system font during ColorSync callback).
- **Wave 68 (PR #415)**: `_handle_list_llm_models` — corrected LM Studio endpoint `/v1/models` → `/api/v1/models` (sister fix to PR #396 which fixed the probe URL). Eliminates silent empty model list in GUI.
- **Wave 69 (PR #417)**: `rest_server.py` — skip GigaAM worker spawn when backend already has a live worker; prevents 1.46 GB duplicate process leak on REST server startup.
- **Wave 73 (PR #420)**: `audio_analytics_service.py` + `call_session_service.py` extracted from `service.py` (8 + ~15 handlers each); continues service extraction pattern to shrink monolith.
- **Wave 266 (AGENT-M fix)**: `BackendToast.show()` AppHang — sister regression to AGENT-K. Root cause: first Cyrillic/emoji message triggered CoreText glyph-metrics build synchronously on main thread → `_doOrderWindow` AppHang. Fix: `prewarmPanel()` pre-warms CoreText cache with representative Cyrillic+emoji string; `show()` now calls `positionPanel()` before `orderFront()`.
- **Wave 274 (v2.0.3 ship)**: tagged release containing Wave 67 SF Symbol fix (AGENT-J), Wave 73 service extractions, Wave 78 +7 error codes, Wave 266 AGENT-M fix, and ~67 waves of tests/hardening shipped since v2.0.2.
- **User manual**: `docs/USER_MANUAL.md` — full end-user guide in Russian (PR #230). Start here for onboarding new users.
- **NSStackView distribution fixes (PRs #228, #239, #240)**: Fixed NSStackView `distribution` property (`.fill` → `.fillEqually` / `.fillProportionally`) for correct layout in Settings + ConversationVC. Actor isolation warnings resolved in ConversationViewController (Swift 6 strict concurrency).

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

**Preferred solution (PR #235)**: use `Krab Ear Dev Local` self-signed identity → TCC grant survives rebuilds. One-time setup:
```bash
scripts/create_local_signing_identity.command   # creates cert in keychain
make sign                                        # rebuilds + signs with stable identity
```
После этого TCC reset после rebuild не нужен — identity hash постоянный.

**Fallback / manual reset** (если stable identity не настроена):

macOS TCC (Accessibility, Microphone) кэширует grants по (bundle-id OR absolute path). После rebuild binary с изменённой hash:
- Старые path-based entries в TCC.db остаются но "смотрят" на stale paths.
- Текущий `com.antigravity.krab-ear` bundle ID **может не совпасть** с историей.
- Симптом: user грантит tumбler, app сразу опять запрашивает permission.

**Quick fix**: `scripts/repair_permissions.command` (PR #234) — автоматизирует шаги 1-4 ниже.

**Diagnostic**: `sqlite3 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" "SELECT client, service, auth_value FROM access WHERE client LIKE '%krab%';"`

**Manual fix workflow** (only do когда user asks):
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

### Dead handler audit methodology (Wave 65 lesson)

When removing dead IPC handlers, a Swift-side grep alone over-counts by ~4×. Full scope required:
1. **Swift callers**: `grep -r "\"method_name\"" native/` — checks HistoryPanel, main.swift, extension files.
2. **Python test dispatch**: `grep -rn "\"method_name\"\|handle_method_name\|assert_dispatch" KrabEar/tests/` — catches test-only callers that verify the handler exists.
3. **Direct Python calls**: `grep -rn "_handle_method_name" KrabEar/` — catches internal calls (cron jobs, batch calls, etc.).
4. Only remove if ALL three are empty. Wave 65 found 177 candidates via Swift-only grep → only 19 confirmed dead after full scope check. Post-Wave 65 batches continued removal, bringing active count from 306 → 296 as of v2.0.3.

### MLX thread-safety in any session

При любой работе с mlx-whisper / MLX-based STT — обязательно оборачивать ALL MLX inference в `with mlx_lock():` context manager. MLX не потокобезопасен и concurrent GPU access вызывает SIGSEGV. (See "MLX thread-safety" in Important Patterns section for details и примеры.)

### Voice Assistant Mode (Phase 1 CLOSED, 2026-04-18)

Phase 1 foundation completed and shipped (2026-04-18, 45+ PRs merged). Core components: Moshi engine, SeamlessStreaming, ConversationViewController, voice triggers, qwen3-30b routing, E2E tests all live. Reference spec: `docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md` (330 lines).

Stack:
- **Engines**: Kyutai Moshi 7B (EN) + SeamlessStreaming 2.5B (RU/ES/multilingual, PyTorch+MPS не MLX).
- **Brain**: `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit` via Krab agent OpenClaw.
- **Orchestration**: Voice Gateway `/v1/sessions/{id}/conversation` WS endpoint.
- **UI**: новый tab "Разговор с AI" в Krab Ear `.app` (`ConversationViewController`).
- **Triggers**: GUI button + Right Option double-tap (300ms) + Silero wake word "Краб".
- **Brain stack**: Krab agent (Telegram userbot) — общая memory + MCP tools + OpenClaw. Voice assistant = "новый channel" в same brain.

Phase 2 (Live Translation), Phase 3 (Call Automation), Phase 4 (STT adapters SenseVoice/Parakeet) — отдельные sub-projects, roadmap в specs/.
