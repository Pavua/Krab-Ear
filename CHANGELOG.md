# Changelog

All notable changes to Krab Ear are documented in this file.

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
