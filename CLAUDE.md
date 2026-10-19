# CLAUDE.md

> Сжато 2026-10-19: длинные уроки/измерения вынесены дословно в [`docs/CLAUDE_ARCHIVE.md`](docs/CLAUDE_ARCHIVE.md) (метка `⟶ [CLAUDE_ARCHIVE §L<n>]` у пункта). Правило и идентификаторы — здесь, история — там.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Before heavy runtime/CI work, open [`docs/AGENT_SKILLS.md`](docs/AGENT_SKILLS.md) for shared agent skill workflows.

## Project Overview

Krab Ear is a local voice assistant/transcriber for macOS. It runs as a two-process system:
- **Native Swift agent** (`native/KrabEarAgent/`) — handles global hotkey (Right Option), UI panel, accessibility paste, and supervises the Python backend via Unix socket IPC.
- **Python backend** (`KrabEar/`) — performs offline STT via `mlx-whisper`, speaker diarization via `pyannote.audio`, translation, and manages transcription history.

The project is bilingual (RU/ES primary, EN secondary). Code comments, UI labels, and docs are in Russian.

## Телефонный STT-профиль Voice Gateway (source, 2026-09-07)

`POST /v1/stt/transcribe` с `request_profile=voice_gateway_call`: RU идёт через
подписываемый IPC `transcribe_ephemeral_call` к единственному уже загруженному
subprocess GigaAM в BackendService. Неизвестный язык сохраняется как явный auto;
фиксированные ES/EN используют общий STT с соответствующим кодом языка.
Все языки профиля идут без history/диаризации/контекста владельца/multipass.
Standalone REST по-прежнему
не создаёт GigaAM. Полный контракт и исходы —
[`docs/IPC_API_REFERENCE.md`](docs/IPC_API_REFERENCE.md#transcribe_ephemeral_call).

`CallSTTService` удерживает отслеживаемую обработку после таймаута клиента;
router/adapter pin запрещает выгрузку/подмену сессии. Privacy читается строго
через `StateStore.call_privacy_mode`; ошибка и повреждение настроек не дают
default OFF. `close()` возвращает False до drain и не закрывает используемые
pipes. Новые source gates не доказывают живую скорость/качество: Gateway-флаги
`KRAB_STT_EAR_CALL_PROFILE_ENABLED` и `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED`
остаются выключенными до отдельной qualification. План:
[`2026-09-07-vg-gigaam-call-profile.md`](docs/superpowers/plans/2026-09-07-vg-gigaam-call-profile.md).

**Оперативный фронт: `docs/NOW.md`** (одна страница). Журнал волн: `docs/ROADMAP-2026H2.md` (обновлять после каждой волны; не очередь задач для исполнителей). Рельсы: `docs/EXECUTOR_PLAYBOOK.md`. Старый `docs/ROADMAP.md` — архив.

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

18 services extracted from `BackendService` — zero orphan imports as of W751 (guarded by CI). W797 additionally split `service.py` into infrastructure files; the live one is `ipc_server.py` (Unix-socket listener). **W1769 reverted the decorative W797 dispatch/logging splits**: the extracted ipc_dispatch.py and service_logging.py modules (no backticks — they no longer exist) were dead drifted duplicates (production used `service.py`'s own inline copies) — both DELETED, dispatch + logging consolidated back into `service.py` as the single source of truth:

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
18. **MeetingSessionService** — живая сессия: GPU-воркер (`meeting_live_speakers_enabled`), `snapshot_range`, `LiveSpeakerTracker` (cosine-сшивка `DiarizeOutput.speaker_embeddings`, не `pyannote/embedding`; cap 16). IPC `meeting_start`/`stop`/`get_meeting_live_state`, события `meeting.*`. Swift: `MeetingLivePanelController` (SSE `/v1/events`, гард `deliverFinished`), `main+MeetingPanel.swift`, `MeetingReportViewController` ⟶ [CLAUDE_ARCHIVE §L89]

Note: `TTSService` (`backend/tts_service.py`) is standalone — not extracted from `service.py`.

Orphan-import regression guard: `scripts/audit_orphan_imports.py` runs in CI on every push.
Decorative-architecture guard: `scripts/audit_decorative_wiring.py --strict` runs in CI (strict, enforced since W1692) — detects service/collaborator instances wired into `self._field` in `__init__` but never called anywhere else (collaborator exists but is a silent no-op). Companion to `audit_orphan_imports` (dangling symbols) + `audit_duplicate_defs` (shadowing) + `audit_cherry_pick_regressions` (body-reverts) + `audit_purge_coverage` (PII purge gaps) + `audit_dead_extracted_modules` (dead extractions). All 9 original findings (W1686) fixed in W1687/W1688/W1690.
Privacy-purge coverage guard: `scripts/audit_purge_coverage.py --fail-on-found` runs in CI (enforced since W1774) — every persisted file-backed store that holds user data must be wiped by `handle_purge_all_data`, or explicitly allowlisted. Found 28 uncovered gaps in W1768; all closed before gate enforcement.
Dead extracted-module guard: `scripts/audit_dead_extracted_modules.py --fail-on-found` runs in CI (enforced since W1774) — no extracted module may have zero production importers, and no cross-file duplicate may exist where the inline copy shadows the extracted one. Root-cause: W797 ipc_dispatch.py + service_logging.py were dead drifted duplicates (deleted in W1769 after guard exposure). Also runs as `make audit-dead-modules` and `make audit-purge-coverage` (both included in `make audit-all`).
Path-containment guard: `scripts/audit_path_containment.py --fail-on-found` runs in CI (enforced since #1674) — no filesystem containment check may be written as a string prefix match (`str(resolved).startswith(str(root))`), which a sibling-prefix path (`/home/user_evil` vs `/home/user`) escapes; must use `Path.relative_to`/`is_relative_to`. Last finding (history_service NDJSON import) fixed in #1677.
Dead in-class handler guard: `scripts/audit_dispatch_test_targets.py --fail-on-found` runs in CI — no in-class `BackendService._handle_<X>` may be a dead shadow of a live extracted dispatch target, and no test may validate the dead copy instead of the live extracted handler ("test-validates-the-hole"). Dead copies masked prod bugs: `get_topic_timeline` DoS (limit≤0) + `get_sentiment_trends`/`get_keyword_cloud` privacy leaks. ⟶ [CLAUDE_ARCHIVE §L98]
Fake-StateStore signature-drift guard: `scripts/audit_fake_store_signatures.py --fail-on-found` (`make audit-fake-store-signatures`) проверяет через AST `state_store.py`. Фейки `StateStore` обязаны принимать ровно те именованные аргументы, которыми их реально зовёт прод (или `**kwargs`) — НЕ всю сигнатуру реального класса (наивный критерий дал 21 ложную находку из 43). Аудит проверяет `def` в фейк-классах и подмену атрибутов (`setattr`, `monkeypatch.setattr`). В CI обязателен `--selftest`. ⟶ [CLAUDE_ARCHIVE §L99]
GigaAM v3: `STT_GIGAAM_MODE`=`v3_e2e_rnnt`; v3-модели ТОЛЬКО из git `salute-developers/GigaAM` (пин `559d88d6` в `scripts/install_gigaam_venv.command`), НЕ из PyPI `gigaam==0.1.0`; голое `"rnnt"`→`v3_rnnt`, указывать полное имя. 🔴 Shortform-лимит 25с: всё `>25.0s` идёт через `AudioChunker`. 🔴 stdout — только однострочный JSON; vendor progress → дренируемый stderr, иначе `_send()` прочитает progress вместо ответа. `PunctuationFixer` не капитализирует «я» внутри предложения. ⟶ [CLAUDE_ARCHIVE §L100]
mlx-masking CI trap (🔴): dev `.venv_krab_ear` is Python 3.14 with mlx-whisper; ubuntu `krabear-ci.yml` is Python 3.12 without mlx wheels, so `import mlx_whisper` tests are false green locally. Validate via `make pre-merge-check` (`scripts/pre_merge_py312_check.sh <files>`); `krabear-ci.yml backend-tests` is the per-PR gate. Swift public jobs on disposable `macos-latest`; MLX/Metal only in private `Pavua/Krab-CI-Control` on trusted SHA; red private device gate is a signal, not a blocker. ⟶ [CLAUDE_ARCHIVE §L101]
SyncThread/TrackingThread atexit hang (🔴 recurring pattern): переопределение `start()` без `super().start()` в наследнике `threading.Thread` оставляет поток в `threading._limbo`, из-за чего `threading._shutdown()` зависает на выходе. **Правило**: в тестах НЕ наследоваться от `threading.Thread`, а делать duck-typing класс `SyncThread` (`start()`, `join()`, `is_alive()`; см. `test_webhook_redirect_ssrf_W1355.py`). ⟶ [CLAUDE_ARCHIVE §L102]
rest_server chunk pollution: `rest_server.py` импортится на module level → кэш `sys.modules`; патч `backend.state_store.StateStore` не переигрывается, `rest_server.store` остаётся реальным (`privacy_mode_enabled=True` → 403). **Fix/Rule**: `setUp/tearDown` + `patch.object` на `backend.rest_server.store`; все патчи/вызовы — к ОДНОМУ module reference в run time (`re-import`, `patch.object(rs, ...)`, `self.rs.ws_stream(...)`). 🔴 При релоаде `app`/`ws_stream` застревают на старом модуле. ⟶ [CLAUDE_ARCHIVE §L103]
Wake-word: hard-negatives ≠ TTS-эхо (🔴): retraining не универсален; для TTS-self-echo — пауза слушателя (`WakeWordPauseReason.ttsPlayback`) до РЕАЛЬНОГО конца воспроизведения (`AVAudioPlayerDelegate.audioPlayerDidFinishPlaying`), НЕ до границы conversation-сессии. Корень: `ConversationErrorAnnouncer` говорит ПОСЛЕ `stopConversation()`, а `AVAudioPlayer.play()` асинхронен после `synthesize_speech`; перекрытие и `play()==false` постят `.krabTTSPlaybackFinished`, иначе пауза зависнет. ⟶ [CLAUDE_ARCHIVE §L104]
Wake-word `_listen_loop` (🔴) — залипание вне `AudioSelfHealer`: `OpenWakeWordAdapter.start()` fire-and-forget, поэтому `wake_word_status` врёт `running:true`; лечит только рестарт backend. **Rule**: `X.is_running()`/`X_status` для потока из голого `Thread.start()` без barrier недостоверен; нужен heartbeat `last_chunk_ts` + watchdog. Фикс: `backend/audio_reinit.py::AudioReinitCoordinator`, `backend/wake_word_watchdog.py::WakeWordWatchdog`, `stop() -> bool`; сторож учитывает `is_recording`. ⟶ [CLAUDE_ARCHIVE §L105]
sys.modules-стабы без снятия (🔴): `_ensure_stubs()` в rest-тестах вставлял фейки (`backend.service`→`_FBS`, `backend.state_store`→`_FSS`, +4) с гардом `if mod_name not in sys.modules` и НИКОГДА не снимал; гард = зависимость от порядка, фейки травили чужие тесты. Rule: module-level стаб ОБРАТИМ — `_ensure_stubs()` возвращает вставленные имена, импорт в `try/finally: sys.modules.pop(name)`; паттерн `_ensure_real_or_stub`. ⟶ [CLAUDE_ARCHIVE §L106]
StateStore._lock (🔴 deadlock): `_lock()` брал `fcntl.flock` на новом fd; flock не привязан к треду → вложенный вход заклинивал. Фикс: per-thread depth-counter (`threading.get_ident()`), flock на внешнем входе; при исключении в фазе захвата счётчик ОТКАТЫВАЕТСЯ (`except BaseException` + rollback + raise). Под `_lock()` не звать колбэки/эмиты, читающие settings/history — копить и эмитить после. Тесты: `test_state_store_lock_reentrancy_W_deadlock_fix.py`, `test_state_store_lock_invariants.py`. ⟶ [CLAUDE_ARCHIVE §L107]
Контенция общего лока: 🔴 `StateStore.load_settings()` берёт `self._lock()` = `history.lock` (`state_store.py:100`) — тот же flock, что охраняет ВСЮ историю. Фикс — `StateStore._lock(nowait=True)`: одна `flock(LOCK_EX|LOCK_NB)` без опроса; fail-closed дефолты с `privacy_mode_enabled=True`. 🔴 **Правило**: health-проба не делит блокирующий ресурс с нагрузкой; Swift `HealthMonitor` (`main+HealthMonitor.swift`) 2.00с, 2 промаха → `forceRestartBackend()`; бюджеты не спасают — только zero-wait. ⟶ [CLAUDE_ARCHIVE §L108]
Privacy-mode gate: any IPC handler that returns transcript text, vocabulary, speaker aliases, or analytics derived from history MUST gate at the top: `if self._cached_settings().get('privacy_mode_enabled'): return <EMPTY_SCHEMA_PARITY_DICT>`. `handle_purge_all_data` wipes all file-backed stores. New handlers that read any of these data categories MUST add the gate. ⟶ [CLAUDE_ARCHIVE §L109]

### Key layers inside `KrabEar/`:
- **`core/config.py`** — Pydantic-Settings singleton (`settings`), all params overridable via `KRAB_EAR_*` env vars. Also contains `DEFAULT_SETTINGS` dict used by UI/IPC.
- **`core/engine.py`** — `AudioEngine`: STT via mlx-whisper with fallback chain (balanced → max candidates → remote), audio normalization, diarization pipeline (pyannote), TTS via macOS `say`.
- **`core/utils.py`** — `TextUtils`: transcript cleanup (soft/strict profiles), hallucination stripping, phrase dedup.
- **`backend/service.py`** — `BackendService`: `handle_request` диспатчит методы через `self._dispatch_table`, строящийся единожды в `__init__` методом `BackendService._build_dispatch_table()` (single source of truth). Все хендлеры (включая `rollback_migration`) инлайн. Потерю импортов и мёртвые модули ловят `scripts/audit_orphan_imports.py` и `scripts/audit_dead_extracted_modules.py`. API: `docs/IPC_API_REFERENCE.md` ⟶ [CLAUDE_ARCHIVE §L115]
- **`backend/ipc_server.py`** — `IPCServer` class: Unix-socket listener, connection accept loop, per-client reader threads. Extracted from `service.py` in W797 phase 2 / W813; inline duplicate removed in #1601 (live, imported by `service.py`).
- **`backend/call_assist_service.py`** — `CallAssistService`: call assist delegation, VoiceGatewayClient integration.
- **`backend/history_service.py`** — `HistoryService`: history CRUD, SRT export, clipboard history, storage info.
- **`backend/translation_service.py`** — `TranslationService`: translate, glossary management, vocabulary suggestions.
- **`backend/settings_service.py`** — `SettingsService`: settings CRUD, profile presets, 5s TTL cache.
- **`backend/recorder.py`** — `AudioRecorder`: thread-safe start/stop audio capture via `sounddevice`. R1 (2026-07-24/25): optional `RecordingSpillWriter` passed into `start(spill=...)` — worker appends raw PCM chunks strictly outside `_lock` (I/O-under-lock is the forbidden W1652/F3 class); `close()` called on every exit path except the `AudioRecorderStopTimeout` raise (worker may still be writing).
- **Владение записью (R2)** — `RecordingCoreService` задаёт `generation_token` и `owner`. 🔴 Статуса `"started"` нет (`MeetingSessionService`). `RecordingStopCoordinator` при `unknown_generation` обязан чистить токен/`isRecording`/ducking (иначе циклится `handleRecordToggleRequest`). Стаб в тестах обязан держать `is_recording=False` до `start()`. Спека: `docs/superpowers/specs/2026-07-25-r2-recording-ownership-design.md`. ⟶ [CLAUDE_ARCHIVE §L122]
- **`backend/recording_spill.py`** — `RecordingSpillWriter` + `finalize_part_to_wav()` (R1): continuous crash-safety spill of an in-progress recording to `<data_dir>/rescue/<uuid>.f32.part` (+ `.meta.json` sidecar), flush per chunk; disk errors fail-open (never break the recording itself, not the safety net). `finalize_part_to_wav` reassembles a `.rescued.wav` on restart.
- **`backend/recording_rescue.py`** — `run_rescue_scan()` (R1): startup scan of `rescue/*.f32.part` left by a prior (crashed) process life — finalizes to WAV, transcribes (privacy_mode-gated), pushes KrabError `audio.recording_rescued`, adds to the "Восстановленные записи" collection. Single-flight, `_MAX_PER_PASS=10`. Accepts an optional `parts=` frozen snapshot (see `service.py` — computed synchronously before the IPC socket exists, closing a race where a live `.part` from a brand-new recording could otherwise be picked up and deleted mid-write).
- **`backend/state_store.py`** — `StateStore`: append-only NDJSON history with tombstone deletes, file-lock, and compaction. Settings stored as settings.json (runtime data file, not in repo).
- **`backend/transcriber.py`** — Thin wrapper over `AudioEngine` for profile/vocabulary management.
- **`backend/translator.py`** — Offline-first translator (RU↔ES, EN→RU, Auto, Bilingual modes) with in-memory cache.
- **`backend/llm_rewriter.py`** — LLM post-processing via LM Studio (qwen3-4b-abliterated). CircuitBreaker + chatbot detection + length ratio guard.
- **`backend/rest_server.py`** — Flask REST API (port 5005) for HTTP-based transcription and metrics. Separate from the IPC service.
- **`backend/rest_inprocess.py`** — `InProcessRestServer`: M2 opt-in (`REST_IN_PROCESS_ENABLED`, default off) — hosts the same REST app inside the backend process on a daemon `make_server()` thread instead of a second `rest_server.py` process; fail-open on port conflict, never breaks dictation.
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
- **`backend/live_subs_service.py`** — `LiveSubsService`: стриминг STT/перевода (`live_subs.result`). Защита от нулевого аудио: `ingest()` и `stop()` не шлют пустой звук (`is_final` возвращает пустой результат, не `None`); `AudioEngine.transcribe` отсекает вход `< _MIN_TRANSCRIBE_SAMPLES` (`_empty_transcription_result`); `_maybe_denoise` и `_guard_denoised` не ломают аудио (`snr_db=0.0` — sentinel). ⟶ [CLAUDE_ARCHIVE §L221]
- **`backend/glossary_auto_learn.py`** — `GlossaryAutoLearn`: auto-extract domain terms (medical etc.) from recent translation history and propose glossary entries.

#### Phase 3 — Call Automation modules:
- **`backend/call_session.py`** — `CallSession` data model + `CallStatus` state machine (`idle→dialing→connected→talking→ending→completed/failed`).
- **`backend/call_session_store.py`** — `CallSessionStore`: persistent storage of call sessions (NDJSON).
- **`backend/call_cost_estimator.py`** — `CallCostEstimator`: compute per-minute telephony cost estimate before dialing; shows ticker during active call.
- **`backend/call_silence_probe.py`** — `CallSilenceProbe`: detect >10 s silence during a call to trigger soft end.
- **`backend/call_auto_end.py`** — `CallAutoEnd`: enforce max-duration limit (default 30 min) and silence-based auto-hangup.
- **`backend/gateway_call_provider.py`** — `GatewayCallProvider`: исходящие звонки через Voice Gateway (`POST /v1/telephony/calls/prompt-call` и `/outbound`) поверх `VoiceGatewayClient`. 🔴 Наружу как `call_control_id` отдаётся **session_id шлюза**, `call_sid` не годится; успех без `session_id` — отказ (`gateway_no_session_id`). 🔴 Клиент возвращает КОНВЕРТ `{ok, payload}`, а формы ответов разные (`GET /v1/sessions` обёрнут, `GET /v1/sessions/{id}` — голый SessionState без `ok`). ⟶ [CLAUDE_ARCHIVE §L230]
- **`backend/observability.py`** — `init_sentry()` / `capture_exception()` helpers; Sentry/GlitchTip SDK init; fully no-op when DSN not provided.
- **`backend/telegram_bridge.py`** — `TelegramBridge`: send messages from Krab Ear backend to main Krab userbot via `POST /api/notify` on localhost web-panel port.
- **`backend/openwakeword_adapter.py`** — `OpenWakeWordAdapter`: Apache-2.0 openWakeWord. `last_detection {model, score, ts=monotonic}` в `wake_word_status`; `settings_get` проброшен из `service.py` (иначе privacy-гейт декоративен); `_privacy_blocked()` проверяется каждый чанк `_listen_loop`. Зависимость опциональна: `KrabEar/requirements-wakeword.txt` (НЕ в requirements.txt) + `openwakeword.utils.download_models()`. ⟶ [CLAUDE_ARCHIVE §L233]

#### Провайдер звонков (после консолидации 03.09.2026):
🔴 Собственные адаптеры (telnyx_adapter, twilio_adapter, sip_local_adapter — без backtick: файлов больше нет, 4723 строки
с тестами) УДАЛЕНЫ: они не были подключены к дороге вызова вовсе — `dial()` не звала ни одна строка
прод-кода, `get_provider` не импортировал ни один модуль, а `call_session_create` писал в журнал звонки,
которых никто не совершал. Линия принадлежит Voice Gateway. Архив — тег `telephony-archive-2026-09-03`.
Спека: `docs/superpowers/specs/2026-09-03-telephony-consolidation.md`.
- **`backend/call_provider.py`** — `CallProvider`: Protocol (structural typing) defining the common interface all telephony adapters must implement.
- **`backend/call_provider_factory.py`** — `get_provider()`: отдаёт `GatewayCallProvider` при `call_provider=gateway` (дефолт) и `NullCallProvider` иначе, с громким предупреждением. 🔴 Старые значения настройки остаются ДОПУСТИМЫМИ в `settings_validator`: выкидывание значения из списка заставляло `set_settings` отвечать ok и молча возвращать прежнее.

#### Additional backend/core modules

Одностроковый каталог остальных `backend/*.py` и `core/*.py` модулей (назначение класса, не контракт) вынесен дословно в
[`docs/CLAUDE_ARCHIVE.md`](docs/CLAUDE_ARCHIVE.md#каталог-модулей-вынесен-дословно). Актуальный список — `ls KrabEar/backend KrabEar/core` + docstring модуля.

- **`backend/shutdown_handler.py`** — `GracefulShutdownHandler`: shutdown info-файл (runtime, вне репо) с `signal`/`pid`; forensic snapshot — `_capture_signal_context()` (signal-safe: no locks/I/O/logging; AST tests в `test_shutdown_info_r1_fields.py`). Реальный OS signal callback — closure `main()` в `service.py` (НЕ `register()`), зовёт `_capture_signal_context()`; `bind()` не трогает `signal.signal()`. `_persist()` перезаписывает `runtime_alive.marker` только после успешной записи. ⟶ [CLAUDE_ARCHIVE §L273]
- **`backend/shutdown_forensics.py`** — `write_alive_marker()` / `check_and_collect()`: `<data_dir>/runtime_alive.marker` удаляется только graceful `_persist()`, переживший = прошлая жизнь умерла UNCLEAN → сбор `log show`/`launchctl print`/лога в `<data_dir>/forensics/<ts>/` (retention 5). `service.py.__init__` спавнит `startup-recovery` только при dirty marker или frozen `.part` rescue candidates; sync-check в `BackendService()` избегает гонки с teardown `TemporaryDirectory` тестов. ⟶ [CLAUDE_ARCHIVE §L274]
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
- **`WakeWordPoller.swift`** — wake word через IPC-поллинг (openWakeWord): `wake_word_start`/`stop`, `wake_word_status` off-main, рост `last_detection.ts` → `triggerConversationFromWakeWord()`. Паузы: `start/stopRealtimeOverlayPolling`, `.krabConversationStarted/Stopped` в `ConversationViewController`, `setPrivacyMode`. 🔴 SSE не подходит: 2 процесса (`service.py` и `rest_server.py`) с раздельными `EventBus` ⟶ [CLAUDE_ARCHIVE §L320]
- **`ErrorBusPoller.swift`** — 🔴 Toast-ошибки требуют IPC-поллинга `list_recent_errors` (`ErrorBus.list_recent_since()` в `backend/error_bus.py`), а не SSE `/v1/events`. `setupErrorBus` (разрыв цикла через `weak var actionHandler`) и `setupHealthMonitor` обязаны вызываться из `completeStartupAfterBackendReady()` в `main.swift` и проверяться source-контракт тестами (`MainErrorsWiringTests.swift`). ⟶ [CLAUDE_ARCHIVE §L321]
- **`main+SparkleUpdater.swift`** — 🔴 Dev-guard: апдейтер НЕ инициализируется, если `.app` находится в каталоге проекта (рядом `KrabEar/backend/service.py`), защищая git-дерево от перезаписи. 🔴 `Sparkle.framework` обязан быть в `Contents/Frameworks/` бандла и `native/Frameworks/` для dev (rpath `@executable_path/../Frameworks` в `Package.swift`); копируют `build_and_deploy.command` и `scripts/assemble_signed_app.sh`. ⟶ [CLAUDE_ARCHIVE §L322]
- **`HotkeyDoubleTapDetector.swift`** — detects Right Option double-tap (300 ms window) to start Voice Assistant conversation.
- **Быстрая заметка + панель-скретчпад**: запись без вставки в окно (`main+QuickCapture.swift`); **`QuickCapturePanelController.swift`** — NSPanel, настройка `quick_capture_show_panel`. 🔴 Ревью ЦЕЛОГО диффа: HIGH sibling-gate asymmetry — `setRecording(true)` только за гейтом автопоказа, `setRecording(false)` за `isVisible`; закрытие мид-записи морозило `isRecording=true`, guard блокирует ресинхронизации. Гейты сняты; `resyncTimerAndPulseIfNeeded()` восстанавливает таймер при `show()`. ⟶ [CLAUDE_ARCHIVE §L324]

#### Phase A — Auto-heal (2026-05-02) Swift additions:
- **`BackendSupervisor.swift`** — двухкольцевой supervisor; passive mode (launchd Variant B = `KeepAlive=true`) или active mode (standalone). Exp backoff restart 0/2/5/15s + circuit breaker (5 fails в 60s window → 5 min cooldown). Spec: `docs/superpowers/specs/2026-05-04-phase-c-roadmap-refinement-design.md` Phase A.
  - **Wave 50 self-recovery bug** (FIXED PR #408): `pgrep + set -e` в launchd plist никогда не работал — `set -e` вызывал немедленный exit при non-zero pgrep exit code. Исправлено в `scripts/install_agent_launchagent.command` (убран `set -e`, добавлен explicit check).
- **`HealthMonitor.swift`** — actor с ping (IPC строго `"ping"`, НЕ `handle_ping`); 2 сбоя → `state = .hung` → однократный `onHangDetected` → `BackendSupervisor.restartIfDeadDetailed()`. В `main+HealthMonitor.swift`: `forceRestartBackend()` под `WedgedEscalationTracker` ТОЛЬКО при `IPCError.socketConnectFailed`, возрасте >600с, живом ping и без записи; `IPCError.timeout` НЕ эскалация. Тесты `HealthMonitorWedgeTests.swift` ⟶ [CLAUDE_ARCHIVE §L329]
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

🔴 **Восстановление IPC после падения backend'а — РОВНО ОДИН механизм** (анти-rebuild,
2026-08-08, PR #1910): `callWithRecovery` / `callAsyncWithRecovery` в
`main+IPCRecovery.swift` — на транспортной ошибке зовут `restartIfDead()` и повторяют
один раз (28 call sites). Не писать второй: конкурент уже был написан однажды
(IPCClient.callWithReconnect, Phase C C.2, без бэктиков — удалён) и прожил три месяца
без единого прод-вызова, потому что слепой бэкофф-сон слабее — мёртвый процесс от
ожидания не воскресает. 🔴 Не подключать ретраи к `HealthMonitor.ping`: его политика
«2 фейла × 3с → hang» рассчитана на быстрый отказ, ретраи внутри вызова растянут тик
до ~20с и убьют детект зависания. Телеметрию восстановления шлёт
`IPCReconnectTelemetry.swift` (`report_reconnect`, params `attempts` + `duration_ms`)
из ОБОИХ путей — при правке одного проверить сиблинг.

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
- **Selected-items export**: `export_selected_items` IPC (`history_service.handle_export_selected_items`) экспортирует ТОЛЬКО `item_ids` в `markdown`/`srt` — `{ok, content, entries, path}`; privacy-gated, non-empty `item_ids`, path-contained under data_dir. Swift `HistoryPanelController+ExportSelection.swift`: IPC off-main (AGENT-3), `presentPanelSheet` (NSSavePanel, no `runModal`); bulk — off-main `delete_history_item`, `list_collections`, `create_collection`, `add_to_collection`. ⟶ [CLAUDE_ARCHIVE §L552]
- **Voice Gateway bridge endpoints**: `Krab Voice Gateway` → STT/TTS Krab Ear (anti-rebuild). `POST /v1/tts/synthesize` зовёт `tts_service.handle_synthesize_speech({text, language, voice?})` → `{wav_bytes_b64, language, engine}`; 403 privacy_mode; engine `say`, Silero/Kokoro при `TTS_ENABLED=1`. `/v1/stream` WS (`@sock.route`, `LiveSubsService.ingest`): `config`/`audio` → `final`, privacy. `backend/cloud_stt.py` — cloud fallback (OpenAI/Deepgram/AssemblyAI), stub `no_api_key`, `timeout=30`. ⟶ [CLAUDE_ARCHIVE §L553]
- **GUI/feature ships** — `scripts/build_and_deploy.command` вызывает `dsymutil $BUILD_BIN -o $DSYM_PATH` (SPM без `.dSYM`). `list_stt_engines` (`STTManagementService.handle_list_stt_engines`) привязан к `HistoryPanelController+STTEnginesPicker.swift`. `get_meeting_report` (`BackendService`, privacy-gated, never-raises) питает `HistoryPanelController+MeetingMode.swift`. Меню: `HistoryPanelController+QuickActions.swift`. ⟶ [CLAUDE_ARCHIVE §L554]
- **🔴 BackendService daemon-thread teardown (ubuntu-chunk flake root)**: `BackendService.__init__` starts daemon threads (`DiskSpaceMonitor`, `RecapScheduler`, `ExportScheduler`); without `self.service.close()` in `tearDown` they hit `_enter_buffered_busy` stderr-lock fatal at shutdown → `exit(1)`, failing the whole chunk. **Rule: every test instantiating `BackendService` MUST call `service.close()` in tearDown.** Reproduce only via in-process multi-file chunk run. ⟶ [CLAUDE_ARCHIVE §L555]
- **`core/pipeline/stt_sherpa.py`** — `SherpaOnnxSTTAdapter`: lazy-load через `_load_lock` (double-checked locking), graceful fallback (`is_available()`=False). MUST be factory-imported в `stt_router_factory.build_router` (`stt_sherpa_enabled`), иначе падает `audit_dead_extracted_modules`. 🔴 Новым модулям `core/pipeline/` обязателен локальный `make audit-all`; тестам нужен `--per-file-ignores` (W293 не ослаблен) ⟶ [CLAUDE_ARCHIVE §L556]
- **Glossary auto-learn**: `get_glossary_suggestions` proposes new glossary entries from recent transcripts; `set_translation_glossary_item` / `remove_translation_glossary_item` manage the glossary.
- **Vocabulary suggestions**: `get_vocabulary_suggestions` proposes STT vocabulary entries from recent history.
- **Diarization on Metal GPU**: pyannote.audio + torch 2.11, device selected via `diarization_device` setting (auto-selects `mps` on Apple Silicon when available).
- **GUI layout**: 3 tabs (Main, History, Settings) with 9 total collapsible sections. Tab state is persisted via NSUserDefaults.
- **Call Assist**: `start_call_assist` / `stop_call_assist` and related IPC methods (`call_assist_diagnostics`, `call_assist_summary`, `call_assist_timeline_*`, `call_assist_quick_phrase`) manage a real-time call translation/assist session.
- **Sentry observability**: `backend/observability.py` wraps `sentry_sdk`. Call `init_sentry(dsn)` at startup (done in `main.py`). All functions are no-op when `dsn` is `None` or empty — safe to ship without a DSN. Swift side: `SentryConfig.init(dsn:)` reads `settings.sentry_dsn` via IPC on launch. Self-hosted GlitchTip (Sentry-compatible) is fully supported.
- **Call provider abstraction**: `CALL_PROVIDER` setting (`telnyx` | `twilio`) selects the active adapter at runtime. Both adapters (`TelnyxAdapter`, `TwilioAdapter`) implement the same interface. Credentials are per-provider settings: `telnyx_api_key` / `twilio_account_sid` + `twilio_auth_token`. Stub mode active when credentials absent.
- **Single-instance guard**: гарантия — только POSIX `flock`. Startup не завершает процессы по PID из-за TOCTOU: в `SingleInstanceGuard.swift` и `main.swift` запрещены `pgrep`, `killOtherAgentInstances`, `SIGKILL`, `SIGTERM`, `kill(`. Тесты `LaunchAgentManagerTests` и `PermissionWizardTests` требуют temp `LaunchAgents` и spy `AutostartManaging`; реальные `~/Library/LaunchAgents` и `/bin/launchctl` запрещены. ⟶ [CLAUDE_ARCHIVE §L564]
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
- **Lazy-load lock**: adapter MUST guard'ить check-then-load per-adapter lock + double-check, иначе double-load (OOM; MLX → SIGSEGV). Locks: `ParakeetSTTAdapter._load_lock` (+`mlx_lock()`), `GigaAMAdapter._spawn_lock`/`_model_lock`. 🔴 КАЖДЫЙ load entry-point: `ParakeetSTTAdapter.warmup()` звал `parakeet_mlx.from_pretrained()` минуя locks и `_load_failed` → SIGSEGV/кирпич. **Rule: каждый `from_pretrained`/model-load site MUST идти через ОДИН locked `_load_model()`, never inline.** ⟶ [CLAUDE_ARCHIVE §L578]

- **🔴 Аварийные сигналы — только `faulthandler`, никогда Python-обработчик**: `signal.signal()` на синхронные сигналы (SIGSEGV/SIGBUS/SIGFPE/SIGILL/SIGABRT) запрещён — CPython зацикливает крэш. Только `faulthandler.enable(all_threads=True)`. В обработчиках запрещены IO/логи: только флаг (`service.main()._signal_handler`, `IPCServer.request_stop_from_signal`). Гейт: `tests/test_fault_signal_handler_2026_08_07.py`. ⟶ [CLAUDE_ARCHIVE §L580]
- **Sentry breadcrumbs (PR #238)**: `backend/observability.py` logs privacy-respecting breadcrumbs (no transcript text, only metadata: method name, duration_ms, error_type). Breadcrumbs auto-attach to next crash report. Pattern: `add_breadcrumb(category="ipc", message="method_name", data={"ok": True})`.
- **Sentry release tracking (PR #241)**: `SentryConfig.swift` reads `CFBundleVersion` and sets `sentry_sdk.set_tag("release", version)` at startup. Enables regression tracking per release in Sentry issues dashboard. Python side sets `release=` in `sentry_sdk.init()`.
- **Stable codesign identity (PR #235)**: `scripts/create_local_signing_identity.command` creates a self-signed cert `Krab Ear Dev Local` in the system keychain. Sign binary with: `codesign -s "Krab Ear Dev Local" -f ...`. TCC grants persist across rebuilds because the identity hash stays constant. **Caveat**: for distribution (App Store / Notarization), replace with Apple Developer ID. See `docs/DEV_CODESIGN.md`.
- **Distribution DMG (PR #229)**: `scripts/build_distribution_dmg.command` creates a signed `.dmg` for sharing. Requires `Krab Ear Dev Local` identity or Apple Developer ID. See `docs/DISTRIBUTION.md`.
- **Analytics UI (PR #231 / #233)**: `AnalyticsDashboardViewController.swift` renders the analytics dashboard via `get_analytics_dashboard` IPC. Shows sentiment trend, quality trend, keyword cloud. Bug fixes in PR #233 (nil guard crash on empty history).
- **IPC full reference**: `docs/IPC_API_REFERENCE.md` — 4341 lines, JSON-RPC handlers documented with params/response schema and examples (PR #243). Active handler count **296** (Wave 65 batch 1 removed 19 dead from 325; subsequent batches brought to 296 as of v2.0.3 — see dead code removal workflow note). Use as ground truth before implementing new IPC calls.
- **Wave 67 (PR #412)**: `StatusIndicatorView.swift` — replaced `●` Unicode literal with SF Symbol `circle.fill` to fix font hang (AGENT-J root cause was CoreText attempting to render Unicode bullet in system font during ColorSync callback).
- **Menu-bar features**: `main+MenuBarRecap.swift` — `MenuBarRecapView` в шапке status-bar меню с сегодняшним `generate_daily_digest`, обновляется в `NSMenuDelegate.menuWillOpen`. `main+StatusDragDrop.swift` — `StatusBarDropView` поверх `statusItem.button`: drop аудио → `transcribe_paths_async` (off-main), BackendToast. Оба: IPC строго off-main (AGENT-3), glyph-guard clean, KrabEarTheme. Меню системное (`statusItem.menu`), чтобы overlay не ломал click-to-open. ⟶ [CLAUDE_ARCHIVE §L588]
- **Wave 68** — каталог LM Studio в `_handle_list_llm_models`: 401 и 3 формата (`/api/v0/models`, `/api/v1/models`, `/v1/models`) давали `has_model=false` в `probe_llm_http` и `None` в `model_loaded`. Разбор — через `lm_studio_lifecycle.parse_lm_studio_catalog` (трёхзначный возврат). Проверка загрузки — только `LOADED_STATE_ENDPOINTS`. Не фильтровать по `type == llm` (`vlm` пригодны), отсекать лишь `embeddings`. ⟶ [CLAUDE_ARCHIVE §L589]
- **Wave 69 (PR #417)**: `rest_server.py` — skip GigaAM worker spawn when backend already has a live worker; prevents 1.46 GB duplicate process leak on REST server startup.
- **Wave 73 (PR #420)**: `audio_analytics_service.py` + `call_session_service.py` extracted from `service.py` (8 + ~15 handlers each); continues service extraction pattern to shrink monolith.
- **Wave 266 (AGENT-M fix)**: `BackendToast.show()` AppHang — sister regression to AGENT-K. Root cause: first Cyrillic/emoji message triggered CoreText glyph-metrics build synchronously on main thread → `_doOrderWindow` AppHang. Fix: `prewarmPanel()` pre-warms CoreText cache with representative Cyrillic+emoji string; `show()` now calls `positionPanel()` before `orderFront()`.
- **Wave 274 (v2.0.3 ship)**: tagged release containing Wave 67 SF Symbol fix (AGENT-J), Wave 73 service extractions, Wave 78 +7 error codes, Wave 266 AGENT-M fix, and ~67 waves of tests/hardening shipped since v2.0.2.
- **NSAlert/NSPanel sheets — NEVER `runModal()`**: a modal loop blocks main thread → AppHang. ALL `NSAlert`/`NSSavePanel`/`NSOpenPanel` MUST use non-blocking helpers in `AlertHelpers.swift`: `presentAlertSheet(_:for:completion:)`, `presentPanelSheet(_:for:completion:)` (`@MainActor`, `window==nil` → log+bail). Guarded by `test_nsAlertRunModal_onlyInAllowlistedFiles` (allowlist `PermissionWizard.swift`; knownSites `DiagnosticsTabView.swift`); new `runModal()` outside allowlist fails CI. ⟶ [CLAUDE_ARCHIVE §L594]
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
- **IPC dispatch error contract**: `handle_request` классифицирует ошибки: `ValueError` и `RuntimeError` дают `invalid_request` + WARNING (валидация/not-found, без спама в Sentry); `IpcOperationalError` (`backend/ipc_errors.py`, ловится первым) даёт `internal_error` в Sentry для IO-сбоев; прочее — `internal_error`. Никогда не слать аварию через голый `RuntimeError`. Тест: `tests/test_dispatch_error_contract.py`. ⟶ [CLAUDE_ARCHIVE §L605]
- **Event-мост IPC→REST**: `backend/event_bridge.py::EventBridge` пересылает события в REST (`:5005`) через `POST /internal/event` (loopback + токен `<data_dir>/event_bridge_token`, 0600, обязателен). `EventBus.emit_envelope()` шлёт SSE/WS без повтора push-листенеров (no-echo guard). Рубильник `event_bridge_enabled`/`KRAB_EAR_EVENT_BRIDGE_ENABLED` читается на старте. Буфер deque(256), stale-TTL 30с (`dropped_stale`) ⟶ [CLAUDE_ARCHIVE §L606]
- **Dual-path item-list collaborators** — `StateStore._load_active_items_unlocked()` возвращает объекты `list[HistoryItem]`, а `get_history_page*()` — словари. Любой обработчик в `core/`/`backend/` ОБЯЗАН быть dual-path (`isinstance(item, dict): item.get(k) else: getattr(item, k)`) или принимать `[x.to_dict() for x in ...]`, иначе падает `'HistoryItem' object has no attribute 'get'`. Грепать `_load_active_items_unlocked`. ⟶ [CLAUDE_ARCHIVE §L607]
- **Live E2E smoke tools** — `scripts/run_e2e_smokes.command` запускает временный dev-бэкенд на temp data-dir и выполняет тесты без риска для прод-истории. `scripts/e2e_ipc_smoke.py` проверяет корректность вывода методов и CRUD, а `scripts/e2e_privacy_gates.py` — отсутствие утечек приватности. 🔴 Dev-сокет = `<data-dir>/krabear.sock` (НЕ `backend.sock`). Запускать после любых правок в `dispatch/handlers`. ⟶ [CLAUDE_ARCHIVE §L608]
- **Session 2026-06-27 — 6 launch-readiness features**: `backend/model_downloader.py` (`download_stt_model`, `get_stt_model_status`, `ModelDownloadStep.swift`); `core/hardware_profile.py` (`get_hardware_profile`); `get_privacy_dashboard`; `StateStore.migrate_history_encryption` (`history_encryption_enabled`); `backend/brain_lease.py` (`~/.openclaw/lm_studio_brain.lock`) в `recording_core_service.py` ⟶ [CLAUDE_ARCHIVE §L609]
- **Session 2026-07-01 — LM Studio self-heal & cloud_rewriter**: HTTP 400 триггерит `lms load` + retry без срыва `CircuitBreaker` (`_try_rest_load`/`_try_rest_unload` идут в CLI, `gemma-4-e4b-it-mlx`, `llm_autoload_timeout_sec`). В `model_downloader`: `cancel_stt_model_download`, watchdog `stt_download_stall_timeout_sec`, снятие `HF_HUB_OFFLINE`. `backend/cloud_rewriter.py`: **`privacy_mode_enabled` ВСЕГДА побеждает** ⟶ [CLAUDE_ARCHIVE §L610]
- **B3 brain-lease видимость**: IPC `get_brain_lease_status {}` → `{ok, enabled, held, owner, pid, acquired_ts, exp_ts, seconds_left}` (`HealthCheckService.handle_get_brain_lease_status`; `_build_brain_lease_summary()` даёт `brain_lease` в `get_diagnostics`); payload чужой — коэрсить, схеме не верить. `main+BrainLease.swift` — строка в меню, в `menuWillOpen` (`refreshQuickNotesSubmenu`, без поллинга); скрыта при `llm_brain_lease_enabled=false`; при IPC-провале «LM Studio: —», не скрывать. ⟶ [CLAUDE_ARCHIVE §L611]
- **S34 clipboard safety**: `PasteService.putToClipboard(_:) -> Bool` — защита: не затирает буфер с `org.nspasteboard.ConcealedType`; Bool ОБЯЗАН проверяться. `pasteToFrontmostApp` — `guard putToClipboard(...) else { return PasteAttemptResult(reason: "concealed_clipboard_skipped") }` ДО key events, иначе Cmd+V вставлял старое защищённое; `appendChunk` — тот же фикс. `handlePasteFailure` — без записи. Явное копирование — `putToClipboardUserInitiated(_:)` без guard. НЕ save/restore-таймер. ⟶ [CLAUDE_ARCHIVE §L612]

- **Внешний контракт Ear (2026-09-17)**: IPC-методы, REST-маршруты и ключи ответов, которые зовут Краб и Voice Gateway, перечислены в `docs/contracts/ear-external-surface.md` и защищены `KrabEar/tests/test_external_surface_contract.py`. Переименование пункта — ломающее изменение: сначала предупредить сессию-потребителя, потом менять.

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

### 🔴 Shell-совместимость гейтов: macOS = Bash 3.2 + BSD-утилиты (2026-07-23, два живых случая)

Два «инструмента, которые выглядели рабочими, но не работали НИКОГДА» — найдены в один день,
класс один: shell-конструкция, валидная в dev-окружении, но не в целевом.
1. **`ensure_agent_running.command`**: `pgrep` на macOS использует **ERE**, а в паттерне стояла
   BRE-альтернация `\|` → искался литерал `KrabEarAgent|native`. 12 подряд `FAIL agent still
   absent` и ни одного успеха при ЖИВОМ агенте (+ лишний `open` при работающем агенте →
   риск дубля против SingleInstanceGuard). Три смок-отчёта подряд диагностировали это как
   «мало 15 секунд» — symptom-fix, который не помог бы.
2. **macOS CI (`ci.yml`) не запускал Python-тесты вообще**: раннер даёт **Bash 3.2**, где нет
   `mapfile`/`readarray` → `files` пуст → все 16 чанков пропущены → job **SUCCESS**. Логи:
   `mapfile: command not found` / `Total test files: 0`. GNU `timeout` на macOS тоже отсутствует
   (нужен `coreutils`/`gtimeout`).

**Правила**: (а) в CI-шагах и `.command`-скриптах для macOS — только Bash 3.2-совместимое
(никаких `mapfile`, `readarray`, `declare -A`, `${var^^}`); (б) всякий цикл «собрать список →
обработать» обязан иметь **fail-closed** гард на пустой список (`[ "$n" -eq 0 ] && exit 1`),
иначе пустота читается как успех; (в) BSD vs GNU: `pgrep`=ERE, `sed -i` требует суффикс,
`timeout` отсутствует — проверяй утилиту в ЦЕЛЕВОЙ ОС, а не только локально;
(г) для «сторожевых» скриптов пиши тест, который поднимает реальный процесс/состояние и
проверяет РЕАЛЬНУЮ утилиту паттерном, извлечённым из скрипта (`test_ensure_agent_running_contract.py`,
`test_safe_backend_restart_contract.py` — образцы).

### 🔴 Рестарт backend в проде — ТОЛЬКО через safe-скрипт (инцидент 2026-07-22)

`launchctl kickstart -k gui/501/ai.krab.ear.backend` под АКТИВНОЙ записью безвозвратно теряет
диктовку (аудио живёт в памяти backend-процесса; GUI получает «Backend не ответил за таймаут»).
Живой инцидент: деплой приёмки codex сделал kickstart ровно в момент диктовки владельца — две
записи потеряны. **Правило: для рестарта прод-backend использовать
`scripts/safe_backend_restart.command` (проверяет `get_recording_state`/`get_meeting_live_state`
через IPC, отказывается под записью/встречей, `--wait N` — подождать, `--with-rest` — + REST-юнит,
после kickstart ждёт ping-готовность). Голый kickstart — только когда сокет уже мёртв/завис**
(скрипт сам это определяет и пропускает проверку). UX-хвост инцидента в основном ЗАКРЫТ волной
R2 «владение записью» (конец июля): `RecordingStopCoordinator` ретраит транспорт (2×) и поллит
`stop_in_progress` (до 5 мин), сохраняет generation-токен (`recordingStopRecoveryPending`) и
повторяет стоп на следующем нажатии хоткея; backend `stop_recording` идемпотентен (replay
терминального ответа по токену), аудио при рестарте спасает R1 rescue-скан («Восстановленные
записи» + тост `audio.recording_rescued`). Оставшаяся дельта (проверено разведкой 2026-08-11):
сохранённый стоп НЕ дострелится сам после реконнекта — только по ручному нажатию.

### CI runner boundary (public Ear / private device gate)

**Исторический контекст 2026-07-24:** repository-level runner `krab-ear-m4max`
в public Ear был введён при account-wide billing block. Это больше не является
приемлемой execution boundary: returning contributor может изменить workflow и
исполнить код на личном Mac, а fork approval не создаёт sandbox.

**Текущая policy после cutover:** public PR/push jobs используют только standard
GitHub-hosted labels из allowlist (`ubuntu-*`, `macos-*`, `windows-*`), без
списков меток, expressions или job-level reusable workflows. `public-runner-
isolation-guard` — regression lint; он не заменяет physical boundary.

Device-specific MLX/Metal gate живёт только в private `Pavua/Krab-CI-Control`
и принимает trusted exact SHA из `codex/krab-ear-v2`. До merge public hosted
workflow public runner обязан быть остановлен, удалён из `Pavua/Krab-Ear`, а
API должен вернуть `total_count=0`. Нельзя снова регистрировать runner в public
repo как recovery shortcut.

Private runner recovery: `scripts/krab_ear_runner_health_check.py` +
`ai.krab.ear.runner-health` проверяют `krab-ear-m4max-private`; source path
будущего service — `~/actions-runner-krab-ci-control`. GITHUB_TOKEN и Telegram
credentials остаются общими владельца и не копируются в controller repository.

### Пульт наблюдения (02.09.2026)

`scripts/status_dashboard.py` — живое состояние экосистемы: прод-процессы,
загрузка машины, движки и задания воркеров ПО ТРЁМ ПРОЕКТАМ, открытые PR,
смерженное за сутки, живые сессии Claude.

```bash
python3 scripts/status_dashboard.py            # текст в терминал
python3 scripts/status_dashboard.py --json     # машинный вид
python3 scripts/status_dashboard.py --serve 8777
```

Постоянная служба: LaunchAgent `ai.krab.ear.dashboard` (RunAtLoad + KeepAlive),
`http://127.0.0.1:8777`. 🔴 Сервер, а не статический файл: данные пересобираются
на КАЖДЫЙ запрос, страница сама обновляется — снимок требовал ручной пересборки
и владелец сразу упёрся в это. Порт **8080 занят панелью главного Краба** из
его release-worktree; трогать оттуда нельзя, ссылка добавляется их стороной.

Воркеры читаются из `<проект>/.remember/tmp/workers/out_*.log`; завершение —
строка `DONE rc=<код>`. 🔴 Класть логи воркеров именно туда, а НЕ в `/tmp`:
перезагрузка машины 02.09 стёрла шесть непрогейченных отчётов. И «ноль байт» ≠
смерть — `cursor-agent` буферизует вывод до конца, поэтому «работает»
определяется по отсутствию маркера `DONE`, а не по размеру файла.

### 🔴 Самоблокировка через передачу работы в пул потоков (класс, 02.09.2026)

Держатель берёт `mlx_lock` в своём потоке и внутри удержания отдаёт работу в
`ThreadPoolExecutor`; рабочий поток берёт ТОТ ЖЕ лок и ждёт того, кто его
породил. `RLock` реентерабелен ТОЛЬКО для своего потока — передача в пул это
ломает. Симптом в проде: `GigaAM-MLX: mlx_lock занят дольше 25.0с` на каждой
итерации превью, «preview worker не завершился за 1.5 с», диктовка 90с на 30с
речи и «Все доступные STT-движки вышли из строя».

Оба известных места закрыты: превью (`transcriber.transcribe_preview` — лок
отпускается сразу после пробы занятости, PR #1972: 1с вместо 90с на живом
замере) и `bulk_reprocess.py` (внешний захват снят целиком, PR #1974). Третьего
сиблинга нет: прочие держатели `mlx_lock` (`audio_lang_id`, адаптеры,
`_transcribe_model`, `scripts/debug_whisper.py`) зовут MLX сами, без делегации.

Снятие в bulk потребовало пройти ВСЕ MLX-пути внутри `engine.transcribe` и
доказать, что каждый берёт лок сам, в своём потоке: `_transcribe_model` (whisper
+ RU-finetune, оба `submit`), GigaAM-MLX `_infer_chunk`, parakeet, voxtral,
`AudioLanguageID._run_detect`, `set_quality_profile`, пост-STT `mx.clear_cache()`;
пути мимо адаптеров и мимо `_transcribe_model` (`_transcribe_remote` — сеть,
диаризация — PyTorch) MLX не трогают.

🔴 **Межпроцессный flock самоблокируется так же** (замерено): другой поток того
же процесса, открыв НОВЫЙ fd на тот же файл, не получает `LOCK_EX` — flock
привязан к описанию открытого файла, не к процессу.

Правило: захват лока перед вызовом, который уходит в другой поток, — всегда
подозрителен. Регресс-тест обязан проверять захват ИЗ ДРУГОГО ПОТОКА: для
своего потока RLock реентерабелен, и баг невидим. Гард на такой инвариант пишем
разбором AST, а не подстрокой: честный комментарий про `mlx_lock` в исправленном
файле роняет подстрочную проверку (напоролись при PR #1974).

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

**Роутинг в `scripts/draft_audit.py`:** **github** (`gh auth token`) — для security. **hf** (`hf_token` из `settings.json`) режет security (403), только нейтральные задачи. Free-ключи хранить в `lens_keys.env` (chmod 600, не коммитить). **nvidia** (`NVIDIA_API_KEY`) логирует I/O → строго code-review ONLY, запрещены приватные данные. Реестр воркеров: `~/.openclaw/krab_runtime_state/free_workers.md`. ⟶ [CLAUDE_ARCHIVE §L774]

**🔴 Уроки:** (1) Security-аудиты проводить ТОЛЬКО на `github/mistral/openrouter` (cerebras/groq/hf режут 403-фильтром). (2) Free-модели — только скауты: каждую находку обязательно верифицировать против реального кода (Claude-гейт), никогда не шипить без проверки. (3) Приватные данные (`mlx-whisper`, голос) — ТОЛЬКО провайдерам без обучения на данных. (4) Anti-rebuild: всегда грепать точное имя перед постройкой. ⟶ [CLAUDE_ARCHIVE §L776]

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

**🔴 Амендмент 2026-08-01 (живой инцидент): стабильная identity НЕ гарантия.**
После пересборки бинаря запись Accessibility может «протухнуть» даже под
`Krab Ear Dev Local`: `AXIsProcessTrusted()` возвращает `true`, агент логирует
«Глобальный hotkey активирован», но события клавиш НЕ приходят (все
NSEvent-мониторы молчат — и Right Option, и Cmd+Shift+N). Переключение
тумблера в System Settings НЕ лечит (меняет флаг у той же протухшей записи).
Лечение: `tccutil reset Accessibility com.antigravity.krab-ear` + перезапуск
агента (`open` с АБСОЛЮТНЫМ путём к бандлу) + согласие в системном диалоге.
Два смежных правила: (1) NSEvent-мониторы проверяют разрешение В МОМЕНТ
СОЗДАНИЯ — выданное позже разрешение требует перезапуска агента; (2) признак
«агент не даёт строк в agent.log при нажатии» при `AX trusted: true` на
старте = именно этот класс. Диагностика: `~/Library/Application Support/KrabEar/agent.log`
(строки `Accessibility AX trusted at startup:` и `Глобальный hotkey активирован`).
Попутный гоч: codesign бинаря ВНУТРИ .app подписывает весь бандл и падает на
«detritus», если на КОРНЕ `Krab Ear.app` висит `com.apple.FinderInfo` —
снимать `xattr -d com.apple.FinderInfo "Krab Ear.app"` (обычный `xattr -cr`
до корня не добирается, а com.apple.provenance безвреден и неудаляем).

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
