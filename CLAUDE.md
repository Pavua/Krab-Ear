# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Krab Ear is a local voice assistant/transcriber for macOS. It runs as a two-process system:
- **Native Swift agent** (`native/KrabEarAgent/`) — handles global hotkey (Right Option), UI panel, accessibility paste, and supervises the Python backend via Unix socket IPC.
- **Python backend** (`KrabEar/`) — performs offline STT via `mlx-whisper`, speaker diarization via `pyannote.audio`, translation, and manages transcription history.

The project is bilingual (RU/ES primary, EN secondary). Code comments, UI labels, and docs are in Russian.

**Актуальный план развития: `docs/ROADMAP-2026H2.md`** (живой документ — статусы волн, приоритеты, post-Fable роутинг моделей; обновлять после каждой волны). Старый `docs/ROADMAP.md` — архив.

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

### Service map (post-W797, 100+ PR marathon)

17 services extracted from `BackendService` — zero orphan imports as of W751 (guarded by CI). W797 additionally split `service.py` into infrastructure files; the live one is `ipc_server.py` (Unix-socket listener). **W1769 reverted the decorative W797 dispatch/logging splits**: the extracted ipc_dispatch.py and service_logging.py modules (no backticks — they no longer exist) were dead drifted duplicates (production used `service.py`'s own inline copies) — both DELETED, dispatch + logging consolidated back into `service.py` as the single source of truth:

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
15. **GlossaryService** — glossary CSV export/import (W772)
16. **LLMOpsService** — list_llm_models, get_last_llm_diff, replace_word_in_last_transcript (W783)
17. **SearchAndAnalysisService** — semantic search + action items + recording analytics (W757)

Note: `TTSService` (`backend/tts_service.py`) is standalone — not extracted from `service.py`.

Orphan-import regression guard: `scripts/audit_orphan_imports.py` runs in CI on every push.
Decorative-architecture guard: `scripts/audit_decorative_wiring.py --strict` runs in CI (strict, enforced since W1692) — detects service/collaborator instances wired into `self._field` in `__init__` but never called anywhere else (collaborator exists but is a silent no-op). Companion to `audit_orphan_imports` (dangling symbols) + `audit_duplicate_defs` (shadowing) + `audit_cherry_pick_regressions` (body-reverts) + `audit_purge_coverage` (PII purge gaps) + `audit_dead_extracted_modules` (dead extractions). All 9 original findings (W1686) fixed in W1687/W1688/W1690.
Privacy-purge coverage guard: `scripts/audit_purge_coverage.py --fail-on-found` runs in CI (enforced since W1774) — every persisted file-backed store that holds user data must be wiped by `handle_purge_all_data`, or explicitly allowlisted. Found 28 uncovered gaps in W1768; all closed before gate enforcement.
Dead extracted-module guard: `scripts/audit_dead_extracted_modules.py --fail-on-found` runs in CI (enforced since W1774) — no extracted module may have zero production importers, and no cross-file duplicate may exist where the inline copy shadows the extracted one. Root-cause: W797 ipc_dispatch.py + service_logging.py were dead drifted duplicates (deleted in W1769 after guard exposure). Also runs as `make audit-dead-modules` and `make audit-purge-coverage` (both included in `make audit-all`).
Path-containment guard: `scripts/audit_path_containment.py --fail-on-found` runs in CI (enforced since #1674) — no filesystem containment check may be written as a string prefix match (`str(resolved).startswith(str(root))`), which a sibling-prefix path (`/home/user_evil` vs `/home/user`) escapes; must use `Path.relative_to`/`is_relative_to`. Last finding (history_service NDJSON import) fixed in #1677.
Dead in-class handler guard: `scripts/audit_dispatch_test_targets.py --fail-on-found` runs in CI (enforced since #1689) — no in-class `BackendService._handle_<X>` may be a dead shadow of a live extracted dispatch target, and no test may validate the dead copy instead of the live extracted handler ("test-validates-the-hole"). Found **36** dead duplicates (34 test-validated) in #1675; all deleted + tests repointed in **#1689**, which surfaced & fixed **3 real prod security bugs the dead copies masked**: `get_topic_timeline` DoS (limit≤0 → unbounded history) + `get_sentiment_trends`/`get_keyword_cloud` privacy-mode leaks (live handlers had lost the gate the dead copy still carried). service.py: 4996→3901 LOC.
mlx-masking CI trap (🔴 recurring root cause): the dev `.venv_krab_ear` is Python 3.14 **with** mlx-whisper installed (macOS wheels exist); ubuntu `krabear-ci.yml` runs Python 3.12 with **no** mlx wheels. Any test asserting `import mlx_whisper` succeeds (or the STT-available branch) is a **false green** locally and fails on ubuntu — caused three red tips. Before merging, validate changed test files with the ubuntu-parity harness: `make pre-merge-check` (or `scripts/pre_merge_py312_check.sh <files>`, #1682) — builds/reuses a py3.12 venv with mlx purged, runs each file memory-safe. ubuntu `krabear-ci.yml backend-tests` (not the macOS `ci.yml` job) is the real gate.
SyncThread/TrackingThread atexit hang (🔴 recurring pattern): any test helper class that inherits `threading.Thread` but overrides `start()` WITHOUT calling `super().start()` leaves the thread in `threading._limbo` (registered but never moved to `_active`). At Python exit, `threading._shutdown()` tries to join all `_limbo` threads → hangs indefinitely → CI exit code 124 (timeout kill at 90s). **Rule**: if you need a synchronous/tracking thread stub in tests, do NOT inherit from `threading.Thread`. Write a pure duck-type class: `class SyncThread: def __init__(self, target=None, args=(), kwargs=None, daemon=None, **kw): ...` with `start()`, `join()`, `is_alive()` methods. See test_webhook_redirect_ssrf_W1355.py for the canonical pattern.
rest_server module-level store chunk pollution (🔴 recurring pattern): `rest_server.py` is imported once at module level in test files using `with patch("backend.state_store.StateStore", return_value=_mock_store)`. When the same test file runs in a CHUNK with other test files (same Python process), `rest_server.py` is CACHED in `sys.modules` from a previous import — the `StateStore` patch does not re-fire. `rest_server.store` remains bound to the REAL StateStore, which may have `privacy_mode_enabled=True` on CI runners, causing `/v1/stt/transcribe` to return `{ok: False, skipped: privacy_mode}` (403) instead of validation errors (400). **Fix**: add `setUp/tearDown` to affected test classes that explicitly patches `backend.rest_server.store` with `_mock_store` using `patch.object`. Example in `test_rest_e2e.py::TranscribeValidationE2ETest`. **🔴 Reload variant (2026-06-16, a9af9b73 red):** a sibling file in the same chunk can RELOAD `backend.rest_server` (swapping `sys.modules`). Any test that binds `app`/`ws_stream`/`_rest_mod` at **module/collection time** then strands them on the OLD module object A, while a **string-target** patch (`patch("backend.rest_server.store", ...)`, `@patch("backend.rest_server.tts_service.handle_synthesize_speech")`) lands on the NEW object B. The handler reads A's stranded globals (a leftover truthy MagicMock store → privacy gate fires; or the real macOS `say` → `FileNotFoundError` on Linux). **Rule**: pin every patch AND the handler/client call to ONE module reference resolved at **run time** — re-import `backend.rest_server` in `setUp` and use `patch.object(rs, ...)` + `self.rs.ws_stream(...)`, or for `_RestBase`-style classes patch `patch.object(_rest_mod, "<attr>", ...)` (the SAME module the test client's `app` came from). A `mock-engine` sentinel + `assert_called_once` makes a bypassed mock fail on macOS too, not only on the Linux-only `say`-absent gate. ubuntu-parity (`pre_merge_py312_check.sh`) isolates each file so it NEVER reproduces this — only the 51-file chunk does; reproduce locally by running the chunk's rest-file prefix in one process.
Wake-word hard-negative training — TTS-эхо ≠ клавиатура (🔴 2026-07-12/13, T5b): дообучение `krab_ru` (openWakeWord) с hard-негативами реальной печати владельца (62→5 ложных срабатываний за 10 мин — почти решено) ОДНОВРЕМЕННО ухудшило устойчивость к СОБСТВЕННОМУ TTS через колонки (0→117 ложных срабатываний за 10 мин на `say -v Milena`) — три независимых прогона (два состава batch-composition, разные random seed) сошлись в один и тот же потолок, дальше крутить веса бессмысленно. Гипотеза (не окончательно доказана, но объясняет паттерн): весь позитивный корпус v1/v2 — 100% синтетический (Silero TTS), маленькая DNN (128 нейронов) частично выучивает «звучит как машинный синтез речи» как proxy-признак вместо чистой фонетики — отсюда всплеск именно на TTS-hard-негативах (тоже синтетическая речь), а не на не-речевом шуме (клавиатура). **Rule**: hard-negative retraining — не универсальный фикс на «ложные срабатывания wake word»; для конкретно TTS-self-echo (приложение само проигрывает голос рядом с своим же микрофоном) архитектурный фикс дешевле и надёжнее модели — приостанавливать wake-word слушатель на время собственного TTS-проигрывания (тот же паттерн, что `RealtimePartialTranscriber.pause()`/`SystemAudioDuckingService`). **Методологическая ловушка эвалюатора**: офлайн-скрипт с одним `Model()` на короткий (<3с) позитивный клип даёт заниженный recall (20% вместо честных 70% по внутреннему гейту auto_train) — скользящий буфер фичей openWakeWord не успевает прогреться за 5-40 чанков; такой харнесс достоверен ТОЛЬКО на длинных непрерывных записях (сотни-тысячи чанков, буфер прогрет) для подсчёта fp, не для recall на коротких клипах.
sys.modules-стабы без снятия (🔴 третий вариант того же chunk-класса, 2026-07-12, красный CI #1871): `_ensure_stubs()` в rest-тестах вставлял фейк-модули (`backend.service`→`_FBS`, `backend.state_store`→`_FSS`, +4) в `sys.modules` НА ИМПОРТЕ файла с гардом `if mod_name not in sys.modules` и НИКОГДА не снимал. Гард — не защита, а зависимость от порядка: в чанке, где до rest-файла никто не импортировал настоящий `backend.service`, фейки оставались и отравляли ВСЕ последующие файлы чанка (26 падений в чужих тестах: `TypeError: _FBS() takes no arguments`, `AttributeError: '_FSS' has no attribute ...`). Любое добавление тест-файлов сдвигает границы чанков и взводит мину. **Rule**: module-level стаб обязан быть ОБРАТИМЫМ — `_ensure_stubs()` возвращает список реально вставленных имён, блок импорта в `try/finally: sys.modules.pop(name)` (rest_server уже связал свои top-level ссылки на фейки — соседям достаются настоящие модули). Починено в 5 файлах (ef3d54ce); 4 других уже использовали безопасный `_ensure_real_or_stub`-паттерн Wave 1744.
StateStore._lock — per-thread реентерабельность + откат при сбое захвата (🔴 production deadlock, #1872): `_lock()` — `fcntl.flock` на НОВОМ fd при каждом входе; flock не привязан к треду → вложенный вход с ТОГО ЖЕ треда самозаклинивал навечно и держал лок для всего StateStore (34 call-site). Реальный триггер: `migrate_history_encryption` держит лок и синхронно зовёт `progress_cb` → `event_bus.emit` → `event_replay._is_privacy_mode` → `cached_settings` (холодный 5с-кэш) → `store.load_settings()` → повторный `_lock()`. Фикс: per-thread depth-counter (`threading.get_ident()`), реальный flock/open/close ровно один раз на внешнем входе/выходе; при исключении в ФАЗЕ ЗАХВАТА (touch/open/flock — ENOSPC/EMFILE реалистичны) инкремент счётчика ОТКАТЫВАЕТСЯ (`except BaseException` + rollback + raise), иначе тред навсегда молча работал бы БЕЗ лока (тише и хуже дедлока). Адверсариальный гейт (Sonnet+Fable) нашёл откат-дыру в первом раунде фикса — при правках `_lock()` прогонять `test_state_store_lock_reentrancy_W_deadlock_fix.py` + `test_state_store_lock_invariants.py` (кросс-процессные инварианты). **Rule для новых долгих операций под `_lock()`**: НЕ звать изнутри колбэки/эмиты, которые могут читать settings/history — либо копить события и эмитить после выхода, либо полагаться на реентерабельность осознанно.
Privacy-mode gate pattern (waves 23-30): any IPC handler that returns transcript text, vocabulary, speaker aliases, or analytics derived from history MUST gate at the top: `if self._cached_settings().get('privacy_mode_enabled'): return <EMPTY_SCHEMA_PARITY_DICT>`. Gates wired in: `sentiment_trends`, `keyword_cloud`, `activity_calendar`, `prepare_share`, `record_playback`, `generate_daily_digest`, `get_analytics_dashboard`, `get_timeline_view`, `extract_action_items`, `get_vocabulary_suggestions`, `get_glossary_suggestions`, `suggest_medical_glossary_terms`, `get_context_memory`, `get_speaker_statistics`, `word_frequency_analysis`, `get_last_llm_diff`, `replace_word_in_last_transcript`. `handle_purge_all_data` wipes all file-backed stores. New handlers that read any of the above data categories MUST add the gate.

### Key layers inside `KrabEar/`:
- **`core/config.py`** — Pydantic-Settings singleton (`settings`), all params overridable via `KRAB_EAR_*` env vars. Also contains `DEFAULT_SETTINGS` dict used by UI/IPC.
- **`core/engine.py`** — `AudioEngine`: STT via mlx-whisper with fallback chain (balanced → max candidates → remote), audio normalization, diarization pipeline (pyannote), TTS via macOS `say`.
- **`core/utils.py`** — `TextUtils`: transcript cleanup (soft/strict profiles), hallucination stripping, phrase dedup.
- **`backend/service.py`** — `BackendService` (business logic hub). `handle_request` dispatches **~322** JSON-RPC methods via `self._dispatch_table` (O(1) dict lookup), which is built **once in `__init__`** by the in-class method `BackendService._build_dispatch_table()` (the single source of truth — the dict literal lives there, after every collaborator/service is constructed), delegating to **17 extracted services** with **zero remaining orphans** (HealthCheckService wired in W751 closed the last gap). Wave 65 batch 1 removed 19 dead handlers; marathon waves W172/W392/W404/W423/W525/W683/W691/W734/W741/W742/W746/W747/W751 added services. **W746 lesson**: the `TextProcessingService` import was silently lost in a W173 rebase — production survived only because Python had the .py loaded. **W750 audit script** (`scripts/audit_orphan_imports.py`) is wired into CI. **W1769**: the decorative W797 dispatch/logging splits were reverted — the extracted ipc_dispatch.py (drifted dead `build_dispatch_table`, never imported in production) and service_logging.py (dead duplicate of the inline `configure_logging`) modules were DELETED (mentioned here without backticks because they no longer exist); `rollback_migration` (stranded in the dead dispatch module since #1592) is now actually live in the inline table. Guarded by `scripts/audit_dead_extracted_modules.py`. Full API reference: `docs/IPC_API_REFERENCE.md` — cross-check live via `grep -cE '"[a-z_]+":\s*self\.' KrabEar/backend/service.py`.
- **`backend/ipc_server.py`** — `IPCServer` class: Unix-socket listener, connection accept loop, per-client reader threads. Extracted from `service.py` in W797 phase 2 / W813; inline duplicate removed in #1601 (live, imported by `service.py`).
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
- **`backend/openwakeword_adapter.py`** — `OpenWakeWordAdapter`: Apache-2.0 wake-word detection (openWakeWord, no email/signup); custom "Краб" model requires ~15 min Jupyter training. НАСТОЯЩИЙ движок wake word с 2026-07-05 (Porcupine удалён): `last_detection {model, score, ts=monotonic}` в `wake_word_status` для IPC-поллинга агента; `settings_get` проброшен из `service.py` (до этого privacy-гейт был декоративным — конструировался без него); `_privacy_blocked()` проверяется каждый чанк `_listen_loop`. Зависимость опциональна: `KrabEar/requirements-wakeword.txt` (намеренно НЕ в requirements.txt — ubuntu-CI ставит его целиком) + однократно `openwakeword.utils.download_models()` (bootstrap_backend.command делает сам).

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
- Resolves project root by checking for `KrabEar/backend/service.py` (порядок: `--project-root` → env `KRAB_EAR_PROJECT_ROOT` → cwd → walk-up от бинаря → указатель `~/Library/Application Support/KrabEar/project_root`, который пишет `scripts/bootstrap_backend.command` — автоустановщик backend для DMG-получателей, вложен в `Contents/Resources` DMG-сборкой; clean-Mac guard в `main.swift` подсвечивает его в Finder).
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
- **`WakeWordPoller.swift`** — wake word через IPC-поллинг backend (openWakeWord): агент шлёт `wake_word_start/stop`, поллит `wake_word_status` (0.75s, off-main), рост `last_detection.ts` → `triggerConversationFromWakeWord()`. Пауза Set-причинами (recording/conversation/privacyMode): хуки в `start/stopRealtimeOverlayPolling`, notification'ы `.krabConversationStarted/Stopped` из `ConversationViewController.start/stopConversation`, `setPrivacyMode`. Self-heal при перезапуске backend (rate-limit 10s). Старый Porcupine-путь (WakeWordListener, файл удалён 2026-07-05, без бэктиков — его больше нет) никогда не работал: заглушка без SDK. 🔴 SSE для wake word НЕ подходит: прод = 2 процесса (`service.py` IPC + `rest_server.py` :5005) с РАЗДЕЛЬНЫМИ EventBus без моста.
- **`ErrorBusPoller.swift`** (2026-07-05, сиблинг-фикс к wake word) — тот же 🔴 2-EventBus гэп касался и toast-уведомлений об ошибках: `ErrorBus.push()` эмиттит `krab_error` только в EventBus IPC-процесса, старый `ErrorSSEBox`/`startErrorBusSSEStream` (main+Errors.swift) слушал SSE `/v1/events` REST-процесса — никогда не срабатывал. Фикс — тот же IPC-поллинг (`list_recent_errors {since_seq}` → только новые ошибки + `latest_seq`, 2s интервал, `ErrorBus.list_recent_since()`/`latest_seq()` в `backend/error_bus.py`). **Отдельная находка при этом же фиксе**: `setupErrorBus(toastPresenter:)` был определён, но НИКОГДА не вызывался из `completeStartupAfterBackendReady()` (декоративная проводка — доккомент врал, что вызывается) из-за циклической зависимости конструирования `ErrorActionHandler`↔`ErrorToastPresenter` (каждому нужен другой при init). Разрыв цикла: `ErrorToastPresenter.actionHandler` стал `weak var` с default `nil` в init, `setupErrorBus` довязывает его постфактум через `as? ErrorToastPresenter` даункаст. Реальный вызов добавлен в `main.swift` (`completeStartupAfterBackendReady`/`applicationWillTerminate`). Весь toast-об-ошибках subsystem был мёртв в проде несмотря на 100% зелёные тесты — тесты (`MainErrorsWiringTests.swift`) тестировали компоненты `setupErrorBus()` в изоляции, не вызывая реальный AgentAppDelegate lifecycle (тот же класс "test-validates-the-hole", что и `audit_dispatch_test_targets.py` для Python). Source-контракт тест добавлен (`test_setupErrorBus_is_actually_called_from_startup`/`test_tearDownErrorBus_is_actually_called_from_shutdown`) по аналогии с Python-стороной. `setupHealthMonitor()` (main+HealthMonitor.swift) — тот же паттерн, тоже был мёртв — **проанализирован и починен в той же сессии сразу вслед**: НЕ дублирует `BackendSupervisor` (тот — актуатор без своего таймера, HealthMonitor — единственный proactive 3s-ping планировщик; реактивный `main+IPCRecovery.swift` срабатывает только на реальном провале IPC-вызова юзера). Без вызова menu-bar status dot никогда не отражал реальное здоровье (только `.stopped`/red default при privacy-toggle). Реальный вызов добавлен в `main.swift`; source-контракт тесты `test_setupHealthMonitor_is_actually_called_from_startup`/`test_tearDownHealthMonitor_is_actually_called_from_shutdown` в `MainHealthMonitorWiringTests.swift`. Известный НЕ тронутый гэп: `subscribeToProbeEvents` (`rewriter_recovered` flash-green) — тот же 2-EventBus паттерн, но чисто косметический, нет готового IPC-метода для поллинг-замены.
- **`main+SparkleUpdater.swift`** (2026-07-05) — автообновления Sparkle 2 (SPM). 🔴 Dev-guard: updater НЕ инициализируется, когда `.app` лежит в каталоге проекта (рядом есть `KrabEar/backend/service.py`) — иначе Sparkle переписал бы git-дерево владельца in-place; работает только для установленных копий (DMG-получатели, /Applications). 🔴 Sparkle — динамический framework: `Sparkle.framework` обязан лежать в `Contents/Frameworks/` бандла (коммитится, как parity-бинарь) и в `native/Frameworks/` для dev-бинаря (gitignored); rpath `@executable_path/../Frameworks` в Package.swift; копирование делают `build_and_deploy.command` и `scripts/assemble_signed_app.sh` (общий ассемблер, используется DMG-скриптом и `.github/workflows/release.yml`). Релиз: тег `vX.Y.Z` или workflow_dispatch → CI-green guard (krab-ear-ci) → GH Release + appcast.xml-коммит `[skip ci]`; подпись — выделенный CI-серт «Krab Ear CI Release» (секреты MACOS_CERT_P12/MACOS_CERT_PASSWORD/SPARKLE_PRIVATE_KEY). Меню «Update Channel» (stable/beta) — по-прежнему вестигиальное, к Sparkle НЕ подключено (один appcast).
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

# Marathon / audit targets (W793)
make audit-orphans            # Orphan import check on service.py (W746/W771 guard)
make audit-orphans ARGS=--strict  # Same but also checks lowercase fn calls
make audit-handlers           # IPC handler complexity report (LOC, cyclomatic, risky calls)
make audit-handlers ARGS=--json  # Machine-readable JSON output
make dispatch-tests           # Run only dispatch-invariant test files (fast gate)
make service-loc              # Print current service.py line count
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
  - **When using `json`**: handlers use `JsonFormatter` defined inline in `backend/service.py::configure_logging` (the single live copy — the W797 service_logging.py extraction was a dead duplicate, deleted in W1769). REST API server (`backend/rest_server.py:280`) emits its own structured records inline via `json.dumps(log_record)` — same `ts/level/...` shape.
  - **Preferred logging pattern**: `logger.info("message", extra={"key": value, ...})` — structured context required for new code. Any non-standard `LogRecord` attribute (i.e. all keys in `extra={...}`) is merged into the JSON output. Standard attrs are filtered via `_STANDARD_LOG_ATTRS` frozenset in `configure_logging`.
  - **Don't use `print()`** in production code. Exceptions: doctest examples, CLI scripts.
- **GitHub Actions CI**: `.github/workflows/ci.yml` runs Python tests (pytest) and Swift build on every push/PR.
- **Profile presets**: four built-in presets (`default`, `meeting`, `translation`, `call_recording`) applied via `apply_profile_preset` IPC method. `list_profile_presets` returns their names/descriptions.
- **Diagnostics**: `get_diagnostics` IPC method returns a structured dict with sections: `system`, `stt`, `llm`, `history`, `settings_cache`. Use for debug panels and status reporting.
- **Metrics dashboard**: `get_metrics_dashboard` returns sliding-window latency percentiles, confidence stats, and diarization usage rate from `MetricsCollector`.
- **Audio device management**: `list_audio_inputs` / `get_audio_devices` enumerate sounddevice inputs; `test_microphone` records a short clip and returns RMS/peak levels. Used by GUI audio device picker.
- **Clipboard history**: last 20 paste items stored in memory; `get_clipboard_history` / `repaste_item` IPC methods. `cleanup_old_history` deletes NDJSON entries older than N days; `get_storage_info` returns file sizes.
- **SRT export**: `export_history_srt` IPC method exports history items as SubRip subtitle file.
- **Selected-items export (2026-06-15)**: `export_selected_items` IPC method (`history_service.handle_export_selected_items`) exports ONLY the given `item_ids` to `markdown`/`srt` — returns `{ok, content, entries, path}`. Privacy-gated, validates non-empty `item_ids`, path-contained under data_dir. Backend shipped + tested (11 tests). Swift UI shipped 2026-06-15 (`HistoryPanelController+ExportSelection.swift`): history-table context menu «Экспортировать выбранное» → `tableView.allowsMultipleSelection`, maps `selectedRowIndexes`→`items[row].id`, IPC off-main (AGENT-3), saves `content` via `presentPanelSheet` (NSSavePanel, no `runModal`). The same context menu also hosts bulk actions (2026-06-15): «Удалить выбранные» (confirm sheet → optimistic descending-index removal → off-main loop `delete_history_item`) and «Добавить в коллекцию…» (off-main `list_collections` → editable `NSComboBox` accessory → `create_collection` if new → off-main loop `add_to_collection`).
- **Voice Gateway bridge endpoints (2026-06-16)**: REST/WS contract so Voice Gateway (sibling project `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway`) drives Krab Ear's STT/TTS (Phase 1.4 foundation; anti-rebuild — reuse Krab Ear services instead of dead Piper). (1) `POST /v1/tts/synthesize` — mirror of `/v1/stt/transcribe`, calls `tts_service.handle_synthesize_speech({text, language, voice?})` → 200 `{wav_bytes_b64, language, engine, byte_count}` | 400 `{error}` | 403 `{ok:false, skipped:privacy_mode}`. Engine=macOS `say` default, Silero(RU)/Kokoro(EN) under `TTS_ENABLED=1`. (2) `/v1/stream` WS (`@sock.route`, flask-sock — already imported in rest_server; reuses `LiveSubsService.ingest`) — streaming ASR: client `{type:config, mode, backend:local|cloud|auto, provider}` + `{type:audio, data:b64 PCM16, sample_rate, is_final}` → server `{type:final, text, lang, translation?}` | `{type:error}`. Privacy-gated (transcript-bearing). (3) `backend/cloud_stt.py` — `backend:"cloud"` cloud-STT fallback, 3 providers (OpenAI `/v1/audio/transcriptions`, Deepgram `/v1/listen`, AssemblyAI), keys from settings `openai_api_key`/`deepgram_api_key`/`assemblyai_api_key`, stub-mode (`no_api_key`) when key absent, all HTTP `timeout=30`.
- **Session 2026-06-18 — 9 GUI/feature ships + CI-stabilization (Sonnet/agy execution, Opus only gated)**: (1) **dSYM root-cause fix** — `scripts/build_and_deploy.command` Step 1 now runs `dsymutil $BUILD_BIN -o $DSYM_PATH`; SPM release builds never emit a `.dSYM` so Step-4 always hit `SKIP_SENTRY` → AppHangs were unsymbolicated. Durable for next release (run `build_and_deploy.command`). (2) **`list_stt_engines` IPC** (`STTManagementService.handle_list_stt_engines`) — enumerates ALL STT engines incl. disabled `{name, display_name, available, enabled, toggle_key, note, type}`+`default`; **Swift** «STT-движки» Settings section (`HistoryPanelController+STTEnginesPicker.swift`, dual Gemini+CD variant) toggles each via `set_settings`. (3) **quick-actions** (`HistoryPanelController+QuickActions.swift`) — history context-menu submenu «Действия с записью»: Резюме (`summarize_item`), Перевести (`translate_text`), →Telegram (`list_telegram_chats` picker → `send_to_telegram {chat_id,text}`); `NSMenuItemValidation` gates to single-selection. (4) **Meeting Mode** — `get_meeting_report {id}` orchestrator IPC (`BackendService._handle_get_meeting_report`: summarize + extract_action_items + `speaker_turns` aggregation → markdown digest; privacy-gated first; never-raises) + Swift `HistoryPanelController+MeetingMode.swift` panel («Открыть как встречу» → sections + Сохранить/Копировать), Gemini-polished `MeetingReportViewController`. (5) **`list_voice_commands` IPC** + «Голосовые команды» Settings section (`HistoryPanelController+VoiceCommands.swift`) — surfaces `voice_commands_enabled`/`voice_commands_strict_mode` (already live in `engine.py`) + command reference from `core.voice_commands._RU/ES/EN_COMMANDS`. (6) «Словарь STT» Settings section (`HistoryPanelController+STTVocabulary.swift`) — manage `stt_hotwords` via `list_/add_/remove_stt_hotword` + `get_vocabulary_suggestions`. (7) **QuickEdit timeout** stepper (`quick_edit_timeout_sec`) next to the QuickEdit toggle. (8) **CI-stabilization (#1782)** — see `feedback_backendservice_teardown_ci` lesson + below.
- **🔴 BackendService daemon-thread teardown (#1782, chronic ubuntu-chunk flake root)**: `BackendService.__init__` starts daemon threads (`DiskSpaceMonitor`, `RecapScheduler`, `ExportScheduler`) that log to stderr; any test creating `BackendService(...)` WITHOUT `self.service.close()` in `tearDown` → at interpreter shutdown those threads hit `_enter_buffered_busy` stderr-lock fatal → process `exit(1)` marking the WHOLE chunk file failed (even with green asserts → "different files fail on different runs"). Fix: `close()` now stops `_disk_monitor`+`_recap_scheduler`; `tearDown(self.service.close())` added to 9 classes. **Rule: every test instantiating `BackendService` MUST call `service.close()` in tearDown.** Also raised `test_metadata_enricher_W1765` ReDoS-timing budget 0.3→2.0s. Reproduce only via in-process multi-file chunk run, never per-file isolation.
- **`core/pipeline/stt_sherpa.py`** — `SherpaOnnxSTTAdapter` (sherpa-onnx Paraformer, ultra-low-latency ASR for calls). Optional (`pip install sherpa-onnx`; `is_available()`=False + graceful when absent). Lazy-load with `_load_lock` double-checked locking (sibling-asymmetry class). Wired in `stt_router_factory.build_router` (opt-in `stt_sherpa_enabled`, mirror Parakeet/SenseVoice) — MUST be factory-imported or `audit_dead_extracted_modules` CI guard fails. 🔴 **Gating lesson (2026-06-16)**: new `core/pipeline/` modules need `make audit-all` locally (dead-module guard not caught by flake8/pytest/ubuntu-parity); test files need flake8 CI-command with `--per-file-ignores` (W293 NOT relaxed in tests).
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
  - **Lazy-load lock pattern (2026-06-14/15, sibling-asymmetry class)**: any adapter that lazy-loads a heavy model MUST guard the check-then-load with a per-adapter lock + double-checked locking, or two concurrent `transcribe()` calls (thread-per-client IPC + the batch queue) double-load it (memory pressure / OOM; for MLX adapters the unguarded `from_pretrained` ALSO races inference on the GPU → SIGSEGV). Established locks: `SenseVoiceSTTAdapter._load_lock` (W1218), `ParakeetSTTAdapter._load_lock` (also wraps the load in `mlx_lock()`), `GigaAMAdapter._spawn_lock` (subprocess) + `_model_lock` (in-process). **Heuristic that found 2 real bugs (parakeet, gigaam): when N similar adapters/services exist and one got a hardening lock but a sibling didn't, grep the fix-class (`_load_lock`/`_spawn_lock`/guard) across all siblings.** 🔴 **W1771 extension (2026-06-17, audit-workflow caught it):** the fix-class spans EVERY load entry-point within an adapter, not just `transcribe()`. `ParakeetSTTAdapter.warmup()` called `parakeet_mlx.from_pretrained()` DIRECTLY — bypassing `_load_lock`, `mlx_lock()`, `mlx_inter_process_lock()` AND `_load_model()`'s transient-vs-permanent `_load_failed` logic — even though `transcribe()` was fully hardened (concurrent `warmup_stt` IPC + a transcribe from another thread → double-load SIGSEGV; a transient `MLXInterLockTimeout` in warmup permanently bricked the adapter). The correct sibling (`SenseVoiceSTTAdapter.warmup()`) routes through `_load_model()` under `_load_lock`. **Rule: every `from_pretrained`/model-load call site (transcribe, warmup, explicit preload) must go through the ONE locked `_load_model()`, never load inline.** Grep `from_pretrained\|_load_model\|warmup` across all adapters when hardening one.

- **Sentry breadcrumbs (PR #238)**: `backend/observability.py` logs privacy-respecting breadcrumbs (no transcript text, only metadata: method name, duration_ms, error_type). Breadcrumbs auto-attach to next crash report. Pattern: `add_breadcrumb(category="ipc", message="method_name", data={"ok": True})`.
- **Sentry release tracking (PR #241)**: `SentryConfig.swift` reads `CFBundleVersion` and sets `sentry_sdk.set_tag("release", version)` at startup. Enables regression tracking per release in Sentry issues dashboard. Python side sets `release=` in `sentry_sdk.init()`.
- **Stable codesign identity (PR #235)**: `scripts/create_local_signing_identity.command` creates a self-signed cert `Krab Ear Dev Local` in the system keychain. Sign binary with: `codesign -s "Krab Ear Dev Local" -f ...`. TCC grants persist across rebuilds because the identity hash stays constant. **Caveat**: for distribution (App Store / Notarization), replace with Apple Developer ID. See `docs/DEV_CODESIGN.md`.
- **Distribution DMG (PR #229)**: `scripts/build_distribution_dmg.command` creates a signed `.dmg` for sharing. Requires `Krab Ear Dev Local` identity or Apple Developer ID. See `docs/DISTRIBUTION.md`.
- **Analytics UI (PR #231 / #233)**: `AnalyticsDashboardViewController.swift` renders the analytics dashboard via `get_analytics_dashboard` IPC. Shows sentiment trend, quality trend, keyword cloud. Bug fixes in PR #233 (nil guard crash on empty history).
- **IPC full reference**: `docs/IPC_API_REFERENCE.md` — 4341 lines, JSON-RPC handlers documented with params/response schema and examples (PR #243). Active handler count **296** (Wave 65 batch 1 removed 19 dead from 325; subsequent batches brought to 296 as of v2.0.3 — see dead code removal workflow note). Use as ground truth before implementing new IPC calls.
- **Wave 67 (PR #412)**: `StatusIndicatorView.swift` — replaced `●` Unicode literal with SF Symbol `circle.fill` to fix font hang (AGENT-J root cause was CoreText attempting to render Unicode bullet in system font during ColorSync callback).
- **Menu-bar features (2026-06-15, agy-Sonnet)**: `main+MenuBarRecap.swift` — `MenuBarRecapView` (NSView) card at the top of the status-bar dropdown showing today's `generate_daily_digest` (tiles + topic chips), refreshed on `NSMenuDelegate.menuWillOpen`. `main+StatusDragDrop.swift` — `StatusBarDropView` (NSDraggingDestination) over `statusItem.button`: drop audio files onto the menu-bar icon → `transcribe_paths_async` (off-main), BackendToast feedback. Both: IPC strictly off-main (AGENT-3), glyph-guard clean, KrabEarTheme tokens. The status menu stays system-managed (`statusItem.menu`) so the drop overlay does not break the click-to-open-menu.
- **Wave 68 (PR #415)**: `_handle_list_llm_models` — corrected LM Studio endpoint `/v1/models` → `/api/v1/models` (sister fix to PR #396 which fixed the probe URL). Eliminates silent empty model list in GUI.
- **Wave 69 (PR #417)**: `rest_server.py` — skip GigaAM worker spawn when backend already has a live worker; prevents 1.46 GB duplicate process leak on REST server startup.
- **Wave 73 (PR #420)**: `audio_analytics_service.py` + `call_session_service.py` extracted from `service.py` (8 + ~15 handlers each); continues service extraction pattern to shrink monolith.
- **Wave 266 (AGENT-M fix)**: `BackendToast.show()` AppHang — sister regression to AGENT-K. Root cause: first Cyrillic/emoji message triggered CoreText glyph-metrics build synchronously on main thread → `_doOrderWindow` AppHang. Fix: `prewarmPanel()` pre-warms CoreText cache with representative Cyrillic+emoji string; `show()` now calls `positionPanel()` before `orderFront()`.
- **Wave 274 (v2.0.3 ship)**: tagged release containing Wave 67 SF Symbol fix (AGENT-J), Wave 73 service extractions, Wave 78 +7 error codes, Wave 266 AGENT-M fix, and ~67 waves of tests/hardening shipped since v2.0.2.
- **NSAlert/NSPanel sheets — NEVER `runModal()` (Sequoia AppHang convention)**: a modal run loop without a parent window blocks the main thread → AppHang (KRAB-EAR-AGENT-E/H class). ALL `NSAlert`/`NSSavePanel`/`NSOpenPanel` presentation MUST use the non-blocking helpers in `AlertHelpers.swift`: `presentAlertSheet(_:for:completion:)` and `presentPanelSheet(_:for:completion:)` (both `@MainActor`, `window==nil` → log+bail). Window source: `self.window` (HistoryPanelController is NSWindowController), `self?.window` inside weak-captured `main.async`, captured `let parentWindow = self.window` where self isn't captured, `NSApp.keyWindow` in AgentAppDelegate. Migrating sync `runModal()`→async sheet moves all post-modal code INTO the completion — watch for code-after-modal that relied on synchronous blocking (e.g. `performGlossaryImport` had to move `syncSettingsControls()` into the completion branches). Guarded by `test_nsAlertRunModal_onlyInAllowlistedFiles` (allowlist: `PermissionWizard.swift` launch-time own run loop; knownSites: `DiagnosticsTabView.swift`). All 26 prior call sites migrated; any new `runModal()` outside the allowlist fails CI.
- **User manual**: `docs/USER_MANUAL.md` — full end-user guide in Russian (PR #230). Start here for onboarding new users.
- **NSStackView distribution fixes (PRs #228, #239, #240)**: Fixed NSStackView `distribution` property (`.fill` → `.fillEqually` / `.fillProportionally`) for correct layout in Settings + ConversationVC. Actor isolation warnings resolved in ConversationViewController (Swift 6 strict concurrency).
- **HistoryItem NaN/Inf guard (wave-28, #1707)**: `HistoryItem.from_dict()` coerces NaN/Inf `confidence`/`duration` to `0.0`. All IPC handlers that compute float fields from history (metadata_enricher, word_timing, speech_pace, noise_profiler, period_comparison, etc.) MUST wrap computed floats with `v if math.isfinite(v) else 0.0` before returning — a single NaN leaks JSON-serialization `null` to Swift and can crash numeric UI components.
- **Concurrent recording lifecycle lock**: `RecordingCoreService._rt_lock` serializes `RealtimePartialTranscriber` and `RealtimeSilenceFilter` start/stop. Any new background daemon wired to recording start/stop MUST be started/stopped under this lock to avoid races with rapid start→stop→start cycles.
- **IPC signing empty-secret guard (wave-31, #1719)**: `request_signing.py` raises `ValueError` when `ipc_signing_secret` is empty/whitespace — HMAC with `b""` is deterministic and trivially forgeable. `settings_service.py` blocks `set_settings` override of signing keys when pinned via `KRAB_EAR_IPC_SIGNING_SECRET` env var.
- **Subprocess interpreter validation (wave-33, #1733)**: `STT_GIGAAM_VENV_PYTHON` (and any similar settings-driven subprocess path) MUST be validated via `Path.resolve().is_relative_to(Path.home())` + basename allowlist `{'python','python3','python3.12',...}` before passing to `subprocess.Popen`. An attacker can `set_settings {STT_GIGAAM_VENV_PYTHON: '/usr/bin/malicious'}` to execute arbitrary local binaries.
- **Obsidian / file-write path containment (wave-32, #1730)**: Any IPC handler writing to a user-configurable path (vault_path, export_path, etc.) MUST validate the resolved path is under `Path.home()` AND not inside known sensitive subdirs (`~/.ssh`, `~/.gnupg`, `Library/Keychains`). Enforce at the IPC boundary (handle_configure), not the internal write path.
- **Sharing/archive path containment (wave-33, #1737)**: File operations using attacker-controlled fields from persisted JSON (e.g., the shares index `filename` key) MUST use `resolved_path.is_relative_to(base_dir)` before unlink/rmtree. A planted `filename='../../x'` enables arbitrary file deletion.
- **Realtime filter settings bounds (wave-34, #1738)**: `rt_silence_check_sec`, `rt_silence_window_sec`, `realtime_silence_threshold_db`, `rt_partial_interval_sec` MUST be clamped in `__init__` AND added to `settings_validator._RANGE_FIELDS`. `Event.wait(timeout≤0)` returns immediately → CPU spin. All new real-time tunable parameters should be added to `_RANGE_FIELDS` with `(min, max)` bounds.
- **Privacy gate completeness (waves 22–34)**: ~30 IPC handlers that return transcript-derived content (keywords, analytics, vocabulary suggestions, call sessions, recording state, semantic search, stats reports, daily digest, learning stats, recording chain/collection content) now gate on `privacy_mode_enabled`. Pattern: `if self._get_runtime_setting('privacy_mode_enabled', False): return EMPTY_SCHEMA_PARITY_DICT` at top of handler. Add gate to ANY new handler that reads history text or derived analytics.
- **IPC dispatch error contract (2026-06-21/22)**: `handle_request` classifies handler exceptions by type — `ValueError` / `RuntimeError` → `invalid_request` + WARNING (these are the codebase's validation/not-found idioms: "Параметр X обязателен", "Элемент не найден: …", ~76 RuntimeError sites — normal user outcomes, must NOT spam Sentry as crashes); `IpcOperationalError` (`backend/ipc_errors.py`, a `RuntimeError` subclass caught FIRST, before the `(ValueError, RuntimeError)` branch) → `internal_error` + `logger.exception` (loud / Sentry) for GENUINE remote/IO failures (VG-gateway down, Telegram-bridge circuit_open/krab_unavailable, disk-write OSError); everything else (AttributeError/KeyError/TypeError/…) → `internal_error` (loud — real bugs). 🔴 **Rule for new handlers**: raise `ValueError`/`RuntimeError` for bad-input/not-found (becomes a quiet `invalid_request`); raise `IpcOperationalError` for an actual operational failure that should page Sentry; never let a bare `RuntimeError` carry a genuine-outage signal. Pinned by `tests/test_dispatch_error_contract.py`.
- **Event-мост IPC→REST (2026-07-07)**: `backend/event_bridge.py::EventBridge` закрывает класс багов «событие эмитится в IPC-процессе, подписчик слушает REST-процесс (`:5005`)» — подписывается на локальную (IPC) шину, батчами (≤20) POST-ит `POST /internal/event` (REST, loopback-only + bridge-токен `<data_dir>/event_bridge_token`, 0600, всегда требуется независимо от `REST_API_AUTH_ENABLED`) → `EventBus.emit_envelope()` доставляет конверт КАК ЕСТЬ существующим SSE/WS подписчикам без повторного вызова push-листенеров (no-echo guard). Killswitch `event_bridge_enabled`/`KRAB_EAR_EVENT_BRIDGE_ENABLED` (default `True`) — читается один раз при старте (сиблинг `DISK_MONITOR_ENABLED`, не live-toggle). Диагностика: `get_diagnostics.event_bridge`. REST недоступен → backoff 1→30с, WARN по смене состояния, эмиттеры не блокируются, deque(256) drop-oldest, stale-TTL 30с (`dropped_stale`) — конверты старше не доставляются задним числом после долгого даунтайма. `main+HealthMonitor.swift` доккомментарий про мёртвый `rewriter_recovered`-гэп удалён — подписка теперь живая. Живой e2e (`scripts/run_e2e_bridge_smoke.command`) доказывает нормальную доставку + хаос (REST kill/recover) + `realtime.partial_transcript` (5-я жертва — streaming paste) отдельно от `krab_error`.
- **Dual-path item-list collaborators (topic_timeline crash class, 2026-06-21)**: `StateStore._load_active_items_unlocked()` returns `list[HistoryItem]` OBJECTS; `get_history_page*()` return DICTS. Any `core/`/`backend/` collaborator that iterates a list of history items MUST be **dual-path** — `_get_field(item)` = `if isinstance(item, dict): item.get(k) else: getattr(item, k)` (see sentiment_trends/timeline_view/keyword_cloud/activity_calendar/recording_insights/stats_report) — OR be fed `[x.to_dict() for x in …]`. A strict-dict collaborator (TopicTracker) fed objects raised `'HistoryItem' object has no attribute 'get'` on EVERY non-empty history (empty short-circuits, so unit tests on empty/mock stores missed it; caught only by the live E2E smoke). Grep `_load_active_items_unlocked` callers when adding an item-list collaborator.
- **Live E2E smoke tools (2026-06-21)**: `scripts/run_e2e_smokes.command` — one command: spins a THROWAWAY dev backend on a temp data-dir (never touches prod/real history), runs both socket smokes, tears down (trap). `scripts/e2e_ipc_smoke.py` = 37 user-facing methods + 5 CRUD round-trips, asserts OUTPUT SANITY (not just keys). `scripts/e2e_privacy_gates.py` = canary: seeds a secret, enables privacy, asserts 20 transcript-methods leak nothing. These catch "feature runs but crashes/leaks/returns wrong data on REAL data" that unit tests (empty/mock store) and static contract audits miss. 🔴 dev socket = `<data-dir>/krabear.sock` (NOT backend.sock). Run after any backend change touching dispatch/handlers.
- **Session 2026-06-27 — 6 launch-readiness features (backend + Swift UI each)**: handler count now **359** (`grep -cE '"[a-z_]+":\s*self\.' KrabEar/backend/service.py`). 7 new IPC methods, 3 new modules, plus brain-lease + DMG memo. (F1) **In-app STT model download** (`download_stt_model {model_id?}` → `{ok,status,model_id}` + `get_stt_model_status {model_id?}` → `{ok,model_id,cached,downloading,status,pct,downloaded,total,error_msg,path}`, EventBus `model_download.progress`) — module `backend/model_downloader.py`; UI `ModelDownloadStep.swift` wired into the onboarding flow (`QuickStartWindowController` in `main.swift`; `PermissionWizard.swift` is now dead/unused). (F2) **Auto-calibration** (`get_hardware_profile {}` → `{ok,chip,ram_gb,cores,tier,is_apple_silicon}` + `get_calibration_recommendation {}` → `{ok,recommended_model,recommended_engine,tier,mic,rationale}`) — module `core/hardware_profile.py`; UI `HistoryPanelController+Calibration.swift` (Apply sets `quality_profile`). (F3) **Privacy dashboard** (`get_privacy_dashboard {}` → `{ok,privacy_mode,encryption_enabled,storage{…},retention{…},audit{…},purge_available}`, counts/flags only — no transcript text, no gate needed) — UI `HistoryPanelController+PrivacyDashboard.swift`. (F4) **Encryption migration** (`migrate_history_encryption {}` → `{ok,status}` bg re-encrypt of plaintext, `.bak` + atomic, event `history_encryption.migrate.progress` + `get_history_encryption_status {}` → `{ok,enabled,total,encrypted,plaintext,pct,migrating}`) — `StateStore.migrate_history_encryption` + `history_encryption_enabled` in `DEFAULT_SETTINGS`; UI migrate button in `HistoryPanelController+SecuritySettings.swift`. (F5) **Brain-lease** — `backend/brain_lease.py` (flock lease `~/.openclaw/lm_studio_brain.lock`, payload `{owner,pid,acquired_ts,exp_ts}`), wired into recording start/stop (release on start / re-acquire on stop) in `recording_core_service.py`; settings `llm_brain_lease_enabled`/`llm_brain_lease_ttl_sec`. Krab-side mirror shipped as a separate-repo patch. (F6) **DMG recipient memo** (`docs/DISTRIBUTION.md`). Plus de-flake of macOS backend tests + accented ES-voice `say` fallback. Docs: `docs/IPC_API_REFERENCE.md` → new "Launch Readiness (2026-06-27)" section.
- **Session 2026-07-01 — LM Studio eviction self-heal + model-download hardening + cloud_rewriter opt-in**: handler count now **360** (`grep -cE '"[a-z_]+":\s*self\.' KrabEar/backend/service.py`; +1 = `cancel_stt_model_download`). (BACKEND-J resolution, #1816) **LLM rewriter self-heals on LM Studio model eviction** — on HTTP 400 "No models loaded" it triggers a synchronous `lms load` + one retry (keeps the CircuitBreaker clean instead of tripping on eviction); `_try_rest_load`/`_try_rest_unload` now detect a `200`+`{"error":...}` body (LM Studio's `/api/v0/models/load` returns 200 with an error payload) and fall back to the CLI. Default rewriter model changed from stale/non-existent `qwen3-4b-abliterated`/`Qwen3-8B-MLX-4bit` → **`gemma-4-e4b-it-mlx`** (real, fast, local). New setting `llm_autoload_timeout_sec` (default 90). (F1 hardening, #1814/#1815) **`model_downloader` (in-app STT download)** — added `cancel_stt_model_download {model_id?}` → `{ok, cancelled, model_id}` IPC (near `download_stt_model`/`get_stt_model_status`) + a stall watchdog (setting `stt_download_stall_timeout_sec`, default 300) + override `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` for the duration of a user-initiated download (prod backend runs offline-hardened, which had blocked fresh installs); `get_stt_model_status` no longer leaks the absolute cache `path`. (#1817 backend + #1818 UI) **`cloud_rewriter` — opt-in CLOUD transcript polishing** (`backend/cloud_rewriter.py`, sibling of `cloud_stt.py`) — fallback in `engine.py` when the LOCAL rewriter returns `ok=False`, making Krab Ear usable WITHOUT a local LLM. Providers: `openai` (gpt-4o-mini), `anthropic` (claude-haiku), `custom` (self-hosted OpenAI-compatible / no-log endpoint via `cloud_rewriter_base_url` + `cloud_rewriter_custom_model` + optional `cloud_rewriter_api_key`, SSRF-guarded). **NO new IPC — settings-driven** (`cloud_rewriter_enabled` default `False`, `cloud_rewriter_provider`). Privacy: gated by `engine._cloud_rewrite_allowed()` where **`privacy_mode_enabled` ALWAYS wins** (transcript never leaves device in privacy mode); privacy-audit logged; no transcript persisted. (tooling) `scripts/draft_audit.py` — per-provider `DEFAULT_MODEL` map (was a single global `gpt-oss-120b` that only worked on cerebras) + HTTP read-timeout raised 120→600s for reasoning models. Docs: `docs/IPC_API_REFERENCE.md` → `cancel_stt_model_download` added to the "Launch Readiness (2026-06-27)" section.

## Non-goals (from PRD)

- Merging Krab/Ear/Voice into a single runtime — they remain separate projects with API boundaries.
- Krab Ear does not implement web scraping; external tool/reasoning goes through OpenClaw gateway.

## Runtime artifact directories (gitignored, local only)

These top-level directories are created at runtime and are excluded from version control via `.gitignore`. Do NOT commit them.

| Directory | Created by | Contents |
|-----------|-----------|----------|
| `.smoke_incidents/` | Smoke-test routines | Timestamped incident `.md` reports (backend down, agent missing, etc.) |
| `dist/` | `scripts/build_distribution_dmg.command` | Distribution DMG builds and staging app copies |
| `logs/` | launchd backend/REST launchagents | `krab-ear-backend.{out,err}.log`, `krab-ear-rest.{out,err}.log` |
| `data/` | E4/E5 runtime | Transcription data dir (dev mode) |
| `.claire/` | Claire agent sessions | Parallel agent session state (same category as `.ralphy/`, `.remember/`) |
| `.coordination/` | Autonomous cycle scripts | Agent boundary snapshots |
| `.benchmarks/` | pytest-benchmark | Benchmark result files |
| `.hypothesis/` | Hypothesis property tests | Auto-managed; has internal `*` gitignore |
| `.ruff_cache/` | ruff linter | Auto-managed; has internal `*` gitignore |

## Working guidelines for Claude sessions

### Sub-agent model selection (cost-conscious)

Используй Agent tool с явным `model` параметром — **default opus сжигает quota** (user установил правило 2026-04-17 после 5h quota hit).

| Model | Use for | % of tasks |
|-------|---------|------------|
| `haiku` | Research, docs, diagnostics, simple edits, memory updates, file reads, grep | ~80% |
| `sonnet` | Implementation PRs, Gemini apply, rebase с conflict resolution, tests, medium refactors | ~18% |
| `opus` | Критический debugging (cascading compiler errors), architectural decisions, когда Sonnet уже failed | ~2% |

Параллелизм > глубина: **многих Haiku параллельно** лучше чем одного Opus linear (5-10× throughput при comparable cost).

### Free-breadth workforce + Claude-gate (🔴 2026-06-17, consolidated Main Krab + Krab Ear)

Claude (Opus/Sonnet) дорогой/быстро кончается → ТОЛЬКО для: брифов, **гейтинга** находок, синтеза, архитектурных решений, единственного критичного HIGH. Весь breadth/исполнение → не-Claude работники. Харнесс: `scripts/draft_audit.py` (fan-out adversarial-аудита одного модуля на free-провайдера; ключи в переменные, НЕ печатать).

**Роутинг (8 провайдеров в `scripts/draft_audit.py`):** 🥇 **github** — ключ `gh auth token` (без отдельного ключа), `gpt-4o-mini`/`gpt-4o`/`Llama-3.1-70B`; лучшая free, **работает для security**. 🥈 **hf** — `hf_token` из KrabEar settings.json (write-scope), router.huggingface.co/v1; 🔴 режет security-промпты (403) → нейтральные задачи. 6 free-тиров (cerebras/groq/mistral/gemini/openrouter/zai) в lens_keys.env (chmod 600, НЕ эхать/коммитить). **nvidia** АКТИВЕН (`NVIDIA_API_KEY` shared via Main Krab) — лучшая `deepseek-ai/deepseek-v4-pro` (поймал баг, что mistral пропустил; 77 моделей), ⚠️ **ЛОГИРУЕТ input/output → code-review ONLY**, НЕ приватные данные. + **agy** (Gemini 3.1 Pro, оплаченный Antigravity) + локальный **LM Studio** (≤10GB, ONE model, 36GB RAM). Кросс-сессионный single-source-of-truth по работникам: `~/.openclaw/krab_runtime_state/free_workers.md` (общий Main Krab / Krab Ear / VG).

**🔴 Уроки (стоили квот):** (1) **cerebras/groq/hf режут security-промпты** (403 content-filter на injection/exploit) → security-аудиты на **github/mistral/openrouter** ONLY; нейтральные (ревью/генерация/доки) → любой. (2) **Free-модели дико переоценивают** (30 кандидатов→0 реальных; gpt-4o-mini точнее, но over-claims + ошибается в фактах И в лекарстве) → **Claude гейтит КАЖДУЮ находку против реального кода**; free = скаут, Claude = гейт; никогда не шипить free-находку без проверки (но гейт извлекает валидное зерно даже из ложной тревоги). (3) **deep-research Claude-workflow = quota-killer** (90 агентов/2.68M токенов сожгли 5ч) → ресёрч через agy-Gemini/web-поиск; ультракод-Workflow-на-каждой-задаче = анти-паттерн для free-breadth. (4) **Privacy-фильтр выбора работника:** code-review (код проекта, в т.ч. Swift) → любой free; приватные данные владельца (STT-транскрипты mlx-whisper, голос) → ТОЛЬКО провайдеры без обучения-на-данных — важнее скорости/цены (в нашем workflow на free уходит КОД, не транскрипт-данные). (5) **Находка ≠ спешный фикс:** rare+self-healing+hot-path-риск → chip; 0 коммитов после волны = валидный исход если ядро здорово; anti-rebuild: грепай точное имя перед постройкой.

**Гейт (Krab Ear = Python-backend + Swift-agent, НЕ чисто Xcode):** Python → `pytest -p no:cacheprovider` + flake8 CI-cmd (W293 не расслаблен) + ubuntu-parity `scripts/pre_merge_py312_check.sh` + chunk-repro для rest-тестов + `make audit-all` для новых `core/pipeline/`; Swift → `swift build -c release`. CI-страж-поллер УБИВАЮТ → `gh run view <id> --json conclusion` напрямую.

### Gemini 3.1 Pro для дизайна (strict rule)

Визуальный дизайн (цвета, шрифты, layout, themes, design tokens) делается **ТОЛЬКО** через Gemini 3.1 Pro, и **ТОЛЬКО** через `agy` (Antigravity CLI) на оплаченной подписке user **Google AI Pro** — НЕ через `gemini` CLI (free OAuth) и НЕ через прямой API-ключ (старый ключ revoked 2026-04-20).
- **Канал (валидирован 2026-06-08):** `agy` → `/opt/homebrew/Caskroom/antigravity-cli/.../antigravity` (v1.0.6). Модель дизайна — `Gemini 3.1 Pro (High)` (список: `agy models`).
- **Инвокация (агентный кодер — сам читает brief, правит Swift, гоняет `swift build`):**
  ```bash
  agy -p "$(cat docs/design-briefs/<brief>.md)\n\nВЫПОЛНИ это ТЗ..." \
    --model "Gemini 3.1 Pro (High)" --dangerously-skip-permissions \
    --add-dir "$(pwd)" --print-timeout 40m < /dev/null > /tmp/krab-ear-gemini/run.log 2>&1
  ```
  - 🔴 **GOTCHA:** в `run_in_background` обязателен `< /dev/null` — иначе `agy -p` виснет на чтении stdin до EOF (симптом: ELAPSED большой, CPU time ~0, 0 правок). Foreground smoke проходит без него.
  - Квота **за запросы** (не токены) → давать крупные пакетные задачи, 1 brief = 1 запрос.
- **Workflow:** Claude пишет brief в `docs/design-briefs/` (что нельзя ломать + что улучшить) → agy исполняет → **Claude ОБЯЗАТЕЛЬНО ревьюит дифф** (`git diff` grep на `runModal`/`sectionId`/переименования контролов/wiring/хардкод-числа) + сам `swift build -c release` → commit с `Co-Authored-By: Gemini 3.1 Pro (Antigravity)` + bundle-binary parity (`Krab Ear.app/Contents/MacOS/KrabEarAgent` + `native/runtime/KrabEarAgent` + codesign) → push.
- Claude НЕ делает визуал сам. Граница: "стало выглядеть иначе" → agy/Gemini; "стало себя вести иначе" → Claude/Sonnet.
- Behavior код (Auto Layout mechanics, ThemeButton tracking areas, state machines, cross-object проводка) — ОК для Claude. В Privacy-индикаторе (2026-06-08) Claude дал agy точную карту проводки в брифе, но рисование замка оставил agy.

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
- **UI**: новый tab "Разговор с AI" в Krab Ear `.app` (`ConversationViewController`). Gemini 3.1 Pro design refresh 2026-06-15 (`ConversationViewController+UI.swift` — статус-бейдж, транскрипт-карточка, токены). Live mic level-meter (`ConversationViewController+LevelMeter.swift` — `MicLevelMeterView`, 20 CALayer-баров с ring-buffer, обновляется из `processAudioSamples` RMS на @MainActor ~12.5/сек; idle-reset из `stopAudioCapture`; НЕ трогает `@Sendable` Core-Audio RT-tap).
- **Triggers**: GUI button + Right Option double-tap (300ms) + Silero wake word "Краб".
- **Brain stack**: Krab agent (Telegram userbot) — общая memory + MCP tools + OpenClaw. Voice assistant = "новый channel" в same brain.

Phase 2 (Live Translation), Phase 3 (Call Automation), Phase 4 (STT adapters SenseVoice/Parakeet) — отдельные sub-projects, roadmap в specs/.
