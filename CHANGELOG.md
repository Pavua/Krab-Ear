# Changelog

All notable changes to Krab Ear are documented in this file.

## [2026-04-18] — Voice Assistant Mode + Phase 4 STT Adapters

### Phase 1: Voice Assistant Mode Foundation
- **#24** feat(ui): ConversationViewController + WebSocket client + UI skeleton
- **#29** feat(ui): Right Option double-tap hotkey + Porcupine wake word "Краб"
- **#34** feat(ui): Voice Assistant section + event handlers в Settings tab (follow-up to #29)
- **#25** docs(plan): Porcupine integration + AVAudioEngine research + CLAUDE.md spec update
- **#27** test(e2e): Phase 1 three-tier contract smoke tests (10/10 passing)
- **#28** test(phase-1.8): E2E fixtures bootstrap + 33-clip matrix specification

### Phase 4: STT Adapters & Model Expansion
- **#23** feat(stt): SenseVoice adapter (RU + emotion detection)
- **#26** feat(stt): Parakeet-TDT-1.1B adapter (EN OpenASR leader)
- **#30** feat(stt): WhisperX adapter (word timestamps + diarization)

### Phase 2: Live Translation (Specification)
- **#31** docs(spec): Phase 2 Live Translation Overlay design (577 lines, UX + integration plan)

### Documentation & Infrastructure
- **#22** docs(spec): Voice Assistant Mode foundation spec + Phase 1 plan + CLAUDE.md guidelines
- **#32** docs(setup): Phase 1 Voice Assistant user setup guide (476 lines, with troubleshooting)
- **#33** feat(tts): Silero (RU) + Kokoro (EN) dual-mode TTS + macOS say fallback

### Cross-Repository Companion PRs
**Krab-Voice-Gateway:**
- #9 feat(conversation): Moshi engine + LazyConversationEngine + WS handler (Phase 1.1)
- #10 docs(claude-md): Document conversation module architecture
- #11 feat(conversation): SeamlessStreaming engine + language routing (Phase 1.2)

**Krab-openclaw:**
- #18 feat(voice): voice_channel_handler + brain proxy + MCP voice tools (Phase 1.4)
- #19 docs(claude-md): Document voice_channel module
- #21 feat(model): qwen3-30b-a3b-2507 routing + LRU eviction (Phase 1.6)

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
