# Release Notes — 2026-04-12

## Summary

Full-session hardening pass across Krab Ear: 33 commits, 104 Sonnet agents, covering GUI expansion, `.app` bundle, major code splits (service.py 4006→1969 lines −51%, HistoryPanelController 4700→2014 main + 8 extensions), 4 extracted services (CallAssist, History, Translation, Settings), security fixes, REST hardening, event contracts wiring, CI/Makefile infrastructure, and a substantial test push (264→411 tests, +56%). 50 files changed, +9488/−5073 lines, 32+ new files.

---

## Features

- **GUI for 15 IPC methods** (`8d751ea`) — Swift `HistoryPanelController` gains dedicated UI controls for diagnostics, profile presets, audio device selection, clipboard history, and history management actions.
- **Live Translation tab keyboard shortcuts + width constraints** (`a0e4228`) — `Cmd+D` (Dictation), `Cmd+E` (Translation), `Cmd+I` (Import); width constraints prevent layout breakage on narrow windows.
- **.app bundle** (`b462eec`, `7701109`, `1f6e165`) — `Krab Ear.app` with `Info.plist`, `LSUIElement` (menu-bar-only mode), codesign; binary updated to latest build after full split.
- **JSON logging, SAY_VOICE validation, VG URL whitelist, CI, Makefile** (`01b3993`) — structured JSON logging pipeline, TTS voice validation at startup, Voice Gateway URL allowlist, GitHub Actions CI workflow, top-level `Makefile` targets.
- **Wire 3 event contracts + VG validation + setupUI split + security fixes** (`0fc058f`) — STT/Translation/Session event contracts plumbed end-to-end; `setupUI` refactored into sub-methods; Voice Gateway URL and auth header hardening.
- **Phase 4 design doc, STT benchmark, app icon, .gitignore, type hints** (`a1fa1ec`) — `docs/phase4_design.md`, latency benchmark script, custom app icon asset, comprehensive `.gitignore`, type annotations across engine and service layers.
- **IPC roundtrip tests, Makefile app/verify/release targets, REST docstrings** (`c036ff7`) — end-to-end IPC socket roundtrip integration tests, new Makefile targets (`make app`, `make verify`, `make release`), OpenAPI-style docstrings on REST endpoints.
- **CallAssistService extraction + 22 unit tests** (`72163c9`) — ~1 013-line module extracted from `backend/service.py` into `backend/call_assist_service.py`; 22 new unit tests, total raised to 286.
- **scroll wrappers for Dictation + Live Translation tabs** (`bf3a738` / `b7b4dbd`) — prevents content clipping on tall content or small window heights.

---

## Refactoring

- **HistoryPanelController → main (2014 lines) + 8 extensions** (`bce82ab`, `4ee0c0b`, `54aeef2`, `46542ec`) — four-stage split: initial 4700→4383 → 4383→3135 (5 extensions) → 3135→2196 (extract +History.swift) → final complete split with +LiveTranslation.swift; main file lands at 2014 lines.
- **service.py: 4006→1969 lines (−51%)** (`c96eb88`, `1bb55fa`, `2a18e00`, `80dec1c`) — removed 997 lines of dead call-assist code; wired `CallAssistService` delegation; extracted `TranslationService` and `SettingsService`; extracted `HistoryService` removing 762 additional dead lines.
- **4 extracted Python services** — `CallAssistService` (`72163c9`), `HistoryService` (`80dec1c`), `TranslationService` + `SettingsService` (`2a18e00`).
- **Deprecated IPC removal, NSTableView delegate move, config tests, REST docs** (`359aa70`) — purged legacy IPC methods, moved `NSTableView` delegate conformance to dedicated extension, added config parity tests, REST endpoint documentation.
- **Remove duplicate function, unused import, empty f-strings, dead variable** (`1534dc1`) — `engine.py` duplicate, `rest_server.py` stale import, `service.py` empty f-strings and dead variable eliminated.
- **History tab scroll + toolbar narrow window layout** (`5d26fb3`) — clipping and overflow fixes for narrow window configurations.

---

## Security

- **Fix path traversal, hardcoded API key, socket permissions** (`054634b`) — `..` traversal in transcript file handler patched; hardcoded LM Studio key replaced with env-var lookup; Unix socket created with `0o600` permissions.
- **File upload validation, error masking, input validation, request logging** (`df494a6`) — REST server now validates MIME type and size for uploaded audio; stack traces masked from API responses; all inputs sanitized; per-request logging added.

---

## Tests

- **HistoryService tests — 13 cases** (`53f5440`) — skip-guarded until extraction; covers all public HistoryService methods.
- **BackendService constructor tests — dispatch table, CallAssist wiring, uptime** (`02f23ae`) — verifies dispatch table completeness, `CallAssistService` delegation contract, and uptime counter initialization.
- **+63 tests across 7 modules (294→357+)** (`1b0deaf`) — new test files covering translator edge cases, state store compaction, metrics sliding window, LLM rewriter circuit breaker, event bus typed dispatch, and config override.
- **EventBus 10 tests + MetricsCollector 11 tests + remove unused urllib imports** (`8ad8f73`) — full coverage of `emit`/`emit_typed`/SSE fan-out and sliding-window percentile math; `urllib` import cleanup.
- **IPC roundtrip tests** (part of `c036ff7`) — integration tests over live Unix socket; verifies request/response envelope format and method dispatch end-to-end.
- **CallAssistService 22 unit tests** (part of `72163c9`) — stubs for all public methods; call-state machine, merge-candidates logic, timeout teardown.
- **TranslationService + SettingsService test contracts** (part of `2a18e00`) — extraction verified against existing test suite with no regressions.

**Final test count: 411 (was 264 at session start, +56%)**

---

## Documentation

- **VG WS reconnect, settings model, error handling, release notes update** (`f44b6dc`) — Voice Gateway WebSocket reconnect docs, settings Pydantic model docs, error handling patterns, prior release notes refreshed.
- **CLAUDE.md expansion — 11 new patterns** (`bf3a738`) — `.app` bundle, `LSUIElement`, iCloud `errno 11` workaround, transcript `.md` generation, LaunchAgent socket path, LLM length-ratio guard, `CollapsibleSectionView` persistence, event contract envelope, config env-var override, test path setup, legacy `AudioEngine` static aliases.
- **Phase 4 design doc** (part of `a1fa1ec`) — `docs/phase4_design.md` covering multi-model delegation, STT fallback chain, and event contract versioning strategy.
- **REST endpoint docstrings** (part of `c036ff7`, `359aa70`) — OpenAPI-style docstrings on all Flask routes.

---

## Infrastructure

- **Makefile: app / verify / release targets** (part of `c036ff7`) — `make app` builds + signs `.app` bundle; `make verify` runs full test suite + lint; `make release` chains both.
- **GitHub Actions CI workflow** (part of `01b3993`) — matrix build on `ubuntu-latest`/`macos-latest`, runs `pytest` with `PYTHONPATH` set, caches `.venv`.
- **.gitignore** (part of `a1fa1ec`) — covers Python cache, `__pycache__`, `*.pyc`, `.venv*`, `*.sock`, `*.log`, `*.ndjson`, macOS `.DS_Store`, Swift `.build/`.
- **chore: update .app bundle binary** (`7701109`, `1f6e165`) — binary refreshed after extension splits to keep `.app` bundle in sync with latest Swift build.

---

## Full Commit List (32 commits)

| Hash | Description |
|---|---|
| `bf8f446` | docs: update CLAUDE.md — 4 extracted services, 411 tests, architecture diagram |
| `80dec1c` | refactor(ear): extract HistoryService + remove 762 dead lines (service.py 2731→1969) |
| `2a18e00` | refactor(ear): extract TranslationService + SettingsService, fix test contracts |
| `f44b6dc` | test+docs: VG WS reconnect, settings model, error handling, release notes update |
| `53f5440` | test(ear): HistoryService tests (13 cases, skip-guarded until extraction) |
| `02f23ae` | test(ear): BackendService constructor tests — dispatch table, CallAssist wiring, uptime |
| `46542ec` | refactor(agent): complete split — +LiveTranslation.swift (8 extensions, main 2014 lines) |
| `c036ff7` | feat+test: IPC roundtrip tests, Makefile app/verify/release, REST docstrings |
| `359aa70` | refactor+test+docs: remove deprecated IPC, NSTableView delegate move, config tests, REST docs |
| `54aeef2` | refactor(agent): extract +History.swift extension (2920→2196 main, +760 lines) |
| `1b0deaf` | test(ear): +63 tests across 7 modules (294→357+ expected) |
| `8ad8f73` | test+fix(ear): EventBus 10 tests + MetricsCollector 11 tests + remove unused urllib imports |
| `01b3993` | feat(ear): JSON logging, SAY_VOICE validation, VG URL whitelist, CI, Makefile |
| `df494a6` | security+feat(rest): file upload validation, error masking, input validation, request logging |
| `0fc058f` | feat(ear): wire 3 event contracts + VG validation + setupUI split + security fixes |
| `1f6e165` | chore: update .app bundle binary after full split |
| `4ee0c0b` | refactor(agent): split HistoryPanelController into 5 extensions (4382→3135 main) |
| `c96eb88` | refactor(ear): remove 997 lines of dead call assist code from service.py |
| `a1fa1ec` | feat(ear): Phase 4 design doc, STT benchmark, app icon, .gitignore, type hints |
| `054634b` | security(ear): fix path traversal, hardcoded API key, socket permissions |
| `1bb55fa` | refactor(ear): wire CallAssistService delegation into BackendService |
| `bce82ab` | refactor(agent): split HistoryPanelController into 2 extension files (4700→4383 lines) |
| `7701109` | chore: update .app bundle binary to latest build |
| `1534dc1` | fix(ear): remove duplicate function, unused import, empty f-strings, dead variable |
| `b7b4dbd` | fix(agent): scroll wrappers for Dictation + Live Translation tabs |
| `bf3a738` | docs: update CLAUDE.md — .app bundle, 11 new patterns, architecture expansion |
| `72163c9` | refactor(ear): extract CallAssistService + 22 unit tests (286 total) |
| `a0e4228` | feat(agent): Live Translation width constraints + keyboard shortcuts (Cmd+D/E/I) |
| `5d26fb3` | fix(agent): History tab scroll + toolbar narrow window layout |
| `b462eec` | feat(ear): .app bundle — Krab Ear.app with Info.plist, codesign, LSUIElement |
| `dfa6f2f` | fix(agent): GUI polish — width constraints for settingsBar sections + uniform button styling |
| `8d751ea` | feat(agent): GUI for 15 IPC methods — diagnostics, profiles, audio, clipboard, history enhancements |

*(32 commits above + `bea6f52` feat(ear): confidence warning + audio devices + test mic + brand expansions = 33 total since session base)*

---

## Stats

| Metric | Before | After | Delta |
|---|---|---|---|
| Tests | 264 | 411 | +56% |
| `service.py` lines | 4006 | 1969 | −51% |
| `HistoryPanelController.swift` main | 4700 | 2014 | −57% |
| Swift extension files | 0 | 8 | +8 |
| Extracted Python services | 0 | 4 | +4 |
| New files | — | 32+ | — |
| Lines changed | — | +9488/−5073 | 50 files |
| Sonnet agents used | — | 104 | — |
| Commits this session | — | 33 | — |

---

## Known Issues

- Test failures on `mlx`, `pyannote`, `sounddevice` imports are pre-existing (dev environment lacks GPU ML deps); not introduced this session.
- `.app` bundle committed as binary — not suitable for GitHub distribution without Git LFS.
