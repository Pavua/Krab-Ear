# Architecture detail

> Moved verbatim from `CLAUDE.md` on 2026-11-22 (context-size cleanup, owner-approved). No text was edited or dropped; `CLAUDE.md` keeps a pointer. Original is also in git history.

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
Журнал звонков `call_sessions.ndjson` (2026-11-15): при `history_encryption_enabled` `CallSessionStore` пишет строки как `ENC1:` тем же `HistoryCrypto`, читает оба формата и НЕ входит в реестр десяти журналов `HISTORY_JOURNAL_FILENAMES` (у снапшотов инвариант 10 файлов). Без ключа запись отклоняется (`CallSessionEncryptionUnavailable`), а не уходит plaintext; `compact()` при нерасшифрованных строках отказывается переписывать файл (иначе потеря ключа стирает журнал); старый открытый файл дошифровывает `migrate_encryption()` (старт backend + IPC `migrate_history_encryption`). `goal_text` в privacy-режиме = `REDACTED`.

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

