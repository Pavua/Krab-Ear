# Changelog

All notable changes to Krab Ear are documented in this file.

## [2026-04-18] — Phase 1 Voice Assistant Mode Complete + Phase 4 STT Adapters

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

## [Unreleased]

### Added
- **Async transcribe with stage-level progress UI** (#15) — backend emits stage events (audio_load → normalize → STT → cleanup → diarize → translate → llm_rewrite); Swift polls every 1s and shows `"файл N/M — <этап>, прошло MM:SS, ETA MM:SS"`. Threaded IPCServer позволяет параллельные запросы во время STT. New `JobTracker`.
- **ThemeButton hover/press/disabled** (#13) — base class with NSTrackingArea-driven interaction states (10% white hover / 15% black + 0.98× press / 40% disabled opacity), все анимации через `Motion.animate()` → Reduce Motion автоматически работает.
- **Liquid Glass на tabs + scroll enclosures** (#11) — прозрачные NSTabView + NSSegmentedControl + 5 NSScrollView.
- **Interaction + Motion + Elevation tokens** (#10) — token foundation via Gemini 3.1 Pro.
- **Spacing migration** (#8) — 60 sites → 4pt grid (The Great 8pt Shift).
- **Colors usage migration** (#7) — 20 sites → semantic tokens.
- **Font system** (#5) — 6 typography tokens.

### Changed
- **`TRANSCRIBE_TIMEOUT_SEC: 300 → 3600`** (#14) — корректное значение для часовых файлов.
- **`NETWORK_MODE` default: `offline_default → offline_strict`** (#14) — без fallback на несуществующий Voice Gateway STT endpoint.
- **`MAX_AUDIO_MB: 50 → 1000`** (#12) — 1-часовые ALAC/AAC звонки укладываются.
- **Swift import report** (#12) — actual backend error messages surface в alert (first 3) + markdown report (`## Errors` section).

### Fixed
- **Autopep8 import breakage** (hotfix 336042f) — `from KrabEar.__version__` → `from __version__`.
- **codesign identifier** (hotfix d9b951c) — `start_agent.command` теперь `com.antigravity.krab-ear`.

### Internal
- **Backend tech debt** (#9) — timeout constants + type hints refactor.
