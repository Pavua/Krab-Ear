# Changelog — Krab Ear

All notable changes to Krab Ear are documented in this file.

**Note (2026-04-18):** Root CHANGELOG consolidated from `/docs/CHANGELOG.md` (archived). For Krab Core (Telegram userbot) architecture, see `PRD-KRAB-CORE.md` and `ARCHITECTURE-KRAB-CORE.md`. For Krab Ear Native documentation, see `docs/PRD-KRAB-EAR.md` and `docs/ARCHITECTURE-KRAB-EAR.md`.

## [2026-04-18] Session III — Crash Recovery + Tech Debt

**Session overview:** 24 merged PRs (19 main round + 5 follow-up). Critical MLX thread-safety SIGSEGV fix (concurrent GPU access serialization). Phase 3 Call Automation design (7 ADR). 112 new unit tests (translator, llm_rewriter.summarize, history_service). Test suite expanded to 4944 tests passing.

### Fixed

- **CRITICAL: MLX thread-safety SIGSEGV** — concurrent GPU access in `mlx_whisper.transcribe` via ThreadPoolExecutor corrupted Metal resource hash table → immediate crash. Global `threading.RLock` in `core/mlx_lock.py` serializes all MLX inference calls. Zero recurrence post-fix. PR #71.

### Added

- **`core/mlx_lock.py`** — reentrant RLock wrapper with context manager for thread-safe MLX operations across concurrent recorders
- **Phase 3 Call Automation ADR** (7 design decisions, architecture document) — PR #65
- **Phase 2.4 E2E tests design** (23 test cases × 7 classes, comprehensive call assist flows) — PR #68
- **+112 unit tests** — translation_service glossary (44), llm_rewriter.summarize (17), history_service CRUD (51) — PR #70
- **7 MLX thread-safety regression tests** — concurrent recorder + GPU load simulation — PR #71
- **Imports hygiene audit report** (0 unused imports, autoflake pass) — PR #69

### Changed

- **`normalize_entities()` optimization** — combined literal-hint fast-path: 2.6–7.6× speedup (inline regex cache, branch prediction). PR #67.
- **Docs consolidation** — CHANGELOG/PRD/ARCHITECTURE root + docs/ merged (PR #66). Deduplicate markdown, single source of truth.
- **ROADMAP_VA session 2 update** — Phase 1 complete, Phase 2.1 backend underway, Phase 4 adapters 4/5 delivered. PR #64.

### Metrics

- **24 PRs merged this session** (19 in main round, 5 follow-up) 
- **4944 unit tests passing** (up from 4482 baseline)
- **MLX crashes: 0 recurrence** after restart (metal resource corruption fully mitigated)
- **Test execution time**: ~24 min on M4 Max (12 cores)

---

## [2026-04-18] Session II — Documentation Consolidation + CI Hardening

**Session overview:** 16 merged PRs. Consolidation of root/docs/ markdown duplicates (CHANGELOG/PRD/ARCHITECTURE). CI hardening via constant refactor and test infrastructure fixes.

### Documentation & Consolidation

- **#48** docs(consolidation): Merge root + docs/ CHANGELOG/PRD/ARCHITECTURE duplicates, add session 2026-04-18 log
- **#62** docs(p1): AA audit resolution — root vs docs/ markdown consolidation (CHANGELOG merged, PRD/ARCHITECTURE unified)

### IPC & Protocol Improvements

- **#50** refactor(ipc): Extract IPC constants (`METHOD_*`, `FIELD_*`) into `backend/ipc_constants.py` for type safety and schema clarity

### Testing Infrastructure & Quality

- **#46** chore(tests): Replace print() with logger in test infrastructure (128 files, consistency + better filtering)
- **#54** refactor(core): Move transcription_scorer logging to proper logger (remove print, use logger.debug)
- **#55** refactor(tests): Standardize all test logging via logger (avoid print in test outputs)
- **#57** refactor(tests): Add formal test contracts for obsidian_sync module
- **#58** refactor(tests): Formalize state_store test suite with integration patterns
- **#60** refactor(tests): Expand translation_service test coverage (glossary, vocabulary, multi-language flows)
- **#61** refactor(tests): Standardize transcriber module tests with mock pipelines
- **#63** fix(backend): Resolve ffmpeg subprocess path for audio conversion (macOS .app env)

### Design & Architecture

- **#56** docs(spec): Phase 2.3 backend architecture update (async event dispatch, job tracking, diagnostics)

### Code Consolidation & Refactoring

- **#47** refactor: Mega consolidation — extract logging, dedupe test utilities, harmonize mock patterns
- **#51** refactor(logging): Audit all logging calls in O/R/S modules, consolidate to logger pattern
- **#52** docs(phase2.1): Design doc for Phase 2.1 backend infrastructure (async, event streaming, diagnostics)
- **#53** refactor(serialization): JsonFormatter consolidation for JSON-RPC and event payloads

### Cleanup & Technical Debt

- **#59** fix(diarization): Pin pyannote.audio version + GPU selection for consistent Metal GPU performance
- **#63** fix(ffmpeg): Resolve subprocess PATH in macOS .app bundle + add diagnostic for missing ffmpeg

---

## [2026-04-17/18] — Phase 1 Voice Assistant Mode Complete + Phase 4 STT Adapters

**Session overview:** 32 merged PRs across 3 repos (Krab-Ear, Voice Gateway, Krab-openclaw) over 2 days. Phase 1 Voice Assistant foundation complete. Phase 4 STT adapters 4/5 delivered. All 4482 tests passing.

### Phase 1: Voice Assistant Mode (Foundation — COMPLETE)

Real-time conversational agent with low-latency speech I/O, local LLM brain, and trigger system.

#### UI & Interaction
- **#24** feat(ui): ConversationViewController + WebSocket client + UI skeleton (Phase 1.3)
- **#29** feat(ui): Right Option double-tap hotkey + Porcupine wake word "Краб" (Phase 1.5)
- **#34** feat(ui): Voice Assistant section + event handlers in Settings tab (Phase 1.5 follow-up)

#### Infrastructure & Engines
- **#25** docs(plan): Porcupine integration + AVAudioEngine research + CLAUDE.md guidelines
- **#27** test(e2e): Phase 1 three-tier contract smoke tests (10/10 passing)
- **#28** test(phase-1.8): E2E fixtures bootstrap + 33-clip matrix specification
- **#31** docs(spec): Phase 2 Live Translation Overlay design (577 lines, complete UX + integration plan)
- **#32** docs(setup): Phase 1 Voice Assistant onboarding guide (476 lines, troubleshooting + manual blockers)
- **#33** feat(tts): Silero (RU) + Kokoro (EN) dual-mode TTS + macOS say fallback (Phase 1.TTS)
- **#41** feat(scripts): Voice Assistant startup + healthcheck + stop scripts

#### Cross-Repo Engine Implementation
**Krab-Voice-Gateway (3 PRs):**
- #9 feat(conversation): Moshi engine + LazyConversationEngine + WebSocket handler (Phase 1.1)
- #10 docs(claude-md): Document conversation module architecture
- #11 feat(conversation): SeamlessStreaming multilingual engine + language routing (Phase 1.2)

**Krab-openclaw (3 PRs):**
- #18 feat(voice): voice_channel_handler + brain proxy + MCP voice tools (Phase 1.4)
- #19 docs(claude-md): Document voice_channel module architecture
- #21 feat(model): qwen3-30b-a3b-instruct-2507 routing + LRU eviction (Phase 1.6)

#### Integration & Spec
- **#22** docs(spec): Voice Assistant Mode foundation spec + Phase 1 plan + CLAUDE.md engineering guidelines

### Phase 4: STT Adapters & Model Expansion

Specialized speech recognition for emotion, timestamps, multilingual, and realtime use cases.

- **#23** feat(stt): SenseVoice adapter (RU + emotion detection) — Phase 4.1
- **#26** feat(stt): Parakeet-TDT-1.1B adapter (EN OpenASR leader) — Phase 4.2
- **#30** feat(stt): WhisperX adapter (word timestamps + diarization) — Phase 4.3
- **#37** feat(stt): Voxtral Mini 4B Realtime adapter (STT + reasoning, RU/ES/EN) — Phase 4.4

### Documentation & Supporting Services

- **#39** docs(research): Consolidate 13 research files into permanent `/docs/research/` with INDEX
- **#43** docs(readme): Document voice assistant setup, 5 STT adapters, TTS, and scripts
- **#45** docs(phase1): Add troubleshooting guide + expected latency metrics for voice assistant

### Infrastructure & Quality

- **#35** docs: ROADMAP_VA.md — public roadmap for Voice Assistant Phase 1–4
- **#36** docs(changelog): April 17–18 session consolidation (32 PRs, Phase 1 VA + Phase 4 adapters)
- **#38** docs(spec): Phase 3 Call Automation design (4–5 PRs planned, TCPA compliance framework)
- **#40** ci(pre-commit): Add flake8 pre-commit hook (prevent F401/W293 CI failures)
- **#42** lint: Remove unused MagicMock+patch imports (flake8 F401)
- **#44** fix(tests): Cap pytest tmp_path retention to failed runs only
- **#46** chore(tests): Replace print() with logger throughout test infrastructure

### Theme & UI System (Liquid Glass — Session D.10a Carryover)

- **#11** fix(theme): Transparent NSTabView + NSSegmentedControl + 5 NSScrollView enclosures
- **#13** feat(ui): ThemeButton hover/press/disabled infrastructure via base class (NSTrackingArea-driven interaction states)
- **#17** feat(theme): Focus ring + transparent hover state refinements
- **#19** feat(theme): Migrate 81 raw NSButton instances to ThemeButton subclasses (Gemini 3.1 Pro design)
- **#21** feat(settings): Diarization toggle + quality profile picker in Audio Pipeline section

### Audio Import & Settings

- **#12** fix(import): MAX_AUDIO_MB 50→1000 (1-hour ALAC/AAC calls supported) + surface backend errors in UI
- **#14** fix(stt): Bump TRANSCRIBE_TIMEOUT_SEC 300→3600 + default NETWORK_MODE=offline_strict (no Voice Gateway STT fallback)
- **#15** feat(import): Async transcribe with stage-level progress UI (audio_load → normalize → STT → cleanup → diarize → translate → llm_rewrite)
- **#16** docs(claude): Note import fixes + ThemeButton hover/press infrastructure
- **#20** feat(backend): Wire PerformanceProfiler into STT/translate/LLM observation paths
- **#25** docs(plan): Porcupine integration + AVAudioEngine research

### Backend & Tooling

- **#18** fix(scripts): Launch .app via LaunchServices (prevent duplicate agent process)

---

## Archive (v2.0.0 — v2.2.0, 2026-04-12 and earlier)

### v2.2.0 — 2026-04-12 (branch: claude/objective-wu, waves 17–18)

Финальный хардинг и расширение покрытия. 41 коммит, 282 файла, +77 013 строк. 168 тест-файлов, 4099 тестов (0 ошибок), 152 Python-модуля, 90 961 строк.

**Новые функции (волны 17–18):**
- **Аудио/STT**: Gain Normalizer, Word Timing, Smart Model Selector, Playback Tracker, Recording Insights (39 тестов)
- **История/хранилище**: Archive Manager, Search History, Activity Calendar, Stats Report Generator, Auto-deduplication, Timeline Export
- **Аналитика**: Health Dashboard, Metadata Enricher, Comparison Module, Export Scheduler, Smart Vocabulary Suggestions
- **Текстовая обработка**: Text Post-Processor (58 тестов)
- **Инфраструктура**: Graceful Shutdown, Startup Diagnostics
- **Тесты**: 4099 тестов (4 пропущено), 168 тест-файлов

---

### v2.1.0 — 2026-04-12 (branch: claude/objective-wu, waves 9–16)

Продолжение крупного цикла разработки. 27 коммитов, 244 файла, +60 027 строк. 148 тест-файлов, 601 IPC-метод, 74 888 строк Python.

**Основные компоненты:**
- **Аудио/STT**: VAD, Noise Profiler, Stage Cache, Recording Merger, Speech Pace Analyzer, Smart silence skip, Calibrator STT
- **Текстовая обработка**: Abbreviation Expander, Anonymizer, Text Chunker, Punctuation Fixer, Term Extractor, Text Comparator
- **История/хранилище**: Transcript Versioning, Collection Manager, Period Comparison, Quality Trends, Integrity Checker, Daily Digest, Obsidian Sync
- **Спикеры**: Speaker Manager, Topic Tracker, Emotion Detector, Sentiment Analysis
- **Аналитика**: Analytics Dashboard, Cost Estimator, HTML Report Generator, Speaker Statistics, Language Learning Integration
- **Инфраструктура**: Transcription Queue, Request Signing, Feature Flags, Retry Strategy, Auto-backup, Audit logging, Webhooks, Plugin system

---

### v2.0.0 — 2026-04-12

Крупный релиз. Полный roadmap закрыт: 16 коммитов, 2114 тестов, 100+ новых компонентов.

**Основные подсистемы:**
- **UI**: Liquid Glass theme, RealtimeOverlay, GUI-кнопки управления AI, 9 коллапсируемых секций
- **STT**: Цепочка фоллбэков (balanced → max → remote), диаризация на Metal GPU, коррекция пунктуации
- **Перевод**: 6 режимов (off, ru_to_es, es_to_ru, en_to_ru, auto, bilingual), глоссарий с авто-обучением, 3 стиля (neutral, chat, formal)
- **LLM**: Интеграция с LM Studio (qwen3-4b-abliterated), circuitbreaker, защиты (chatbot guard, length ratio)
- **История**: Append-only NDJSON, теги, коллекции, избранное, буфер обмена (20 последних), fuzzy-поиск
- **Экспорт**: SRT, Markdown, CSV, JSON, Obsidian, Batch-экспорт
- **REST API**: OpenAPI/Swagger, Bearer-токен auth, Rate limiting, SSE/WebSocket, Prometheus-метрики
- **Call Assist**: Live-трансляция звонков, timeline, quick phrases, LLM-суммари
- **Аналитика**: MetricsCollector (latency p50/p95/p99), дашборд метрик, статистика использования, Error reporter
- **Swift-агент**: 7 extension-файлов, LaunchAgentManager, SystemAudioDuckingService, NotificationService, PermissionWizard
- **Тесты**: 2114 тестов с полным охватом AudioEngine, BackendService, всех IPC-методов
