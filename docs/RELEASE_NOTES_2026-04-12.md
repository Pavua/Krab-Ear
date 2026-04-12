# Release Notes — 2026-04-12

## Summary

Full-session hardening pass across Krab Ear: 28+ commits, 90+ Sonnet agents, covering GUI expansion, `.app` bundle, major code splits (service.py 4006→2924 lines, HistoryPanelController 4700→2014 main + 8 extensions), security fixes, REST hardening, event contracts wiring, CI/Makefile infrastructure, and a substantial test push (264→377+ tests).

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

---

## Refactoring

- **HistoryPanelController → main (2014 lines) + 8 extensions** (`bce82ab`, `4ee0c0b`, `54aeef2`, `46542ec`) — four-stage split: initial 4700→4383 → 4383→3135 (5 extensions) → 3135→2196 (extract +History.swift) → final complete split with +LiveTranslation.swift; main file lands at 2014 lines.
- **service.py: 4006→2924 lines** (`c96eb88`, `1bb55fa`) — removed 997 lines of dead call-assist code; wired `CallAssistService` delegation into `BackendService` constructor.
- **Deprecated IPC removal, NSTableView delegate move, config tests, REST docs** (`359aa70`) — purged legacy IPC methods, moved `NSTableView` delegate conformance to dedicated extension, added config parity tests, REST endpoint documentation.
- **Remove duplicate function, unused import, empty f-strings, dead variable** (`1534dc1`) — `engine.py` duplicate, `rest_server.py` stale import, `service.py` empty f-strings and dead variable eliminated.

---

## Security

- **Fix path traversal, hardcoded API key, socket permissions** (`054634b`) — `..` traversal in transcript file handler patched; hardcoded LM Studio key replaced with env-var lookup; Unix socket created with `0o600` permissions.
- **File upload validation, error masking, input validation, request logging** (`df494a6`) — REST server now validates MIME type and size for uploaded audio; stack traces masked from API responses; all inputs sanitized; per-request logging added.

---

## Tests

- **+63 tests across 7 modules (294→357+)** (`1b0deaf`) — new test files covering translator edge cases, state store compaction, metrics sliding window, LLM rewriter circuit breaker, event bus typed dispatch, and config override.
- **EventBus 10 tests + MetricsCollector 11 tests + remove unused urllib imports** (`8ad8f73`) — full coverage of `emit`/`emit_typed`/SSE fan-out and sliding-window percentile math; `urllib` import cleanup.
- **BackendService constructor tests — dispatch table, CallAssist wiring, uptime** (`02f23ae`) — verifies dispatch table completeness, `CallAssistService` delegation contract, and uptime counter initialization.
- **IPC roundtrip tests** (part of `c036ff7`) — integration tests over live Unix socket; verifies request/response envelope format and method dispatch end-to-end.
- **CallAssistService 22 unit tests** (part of `72163c9`) — stubs for all public methods; call-state machine, merge-candidates logic, timeout teardown.

**Final test count: 377+ (was 264 at session start)**

---

## Documentation

- **CLAUDE.md expansion — 11 new patterns** (`bf3a738`) — `.app` bundle, `LSUIElement`, iCloud `errno 11` workaround, transcript `.md` generation, LaunchAgent socket path, LLM length-ratio guard, `CollapsibleSectionView` persistence, event contract envelope, config env-var override, test path setup, legacy `AudioEngine` static aliases.
- **CLAUDE.md update: .app bundle, architecture expansion** (part of `bf3a738`) — architecture diagram updated to reflect current layer boundaries post-split.
- **Phase 4 design doc** (part of `a1fa1ec`) — `docs/phase4_design.md` covering multi-model delegation, STT fallback chain, and event contract versioning strategy.
- **REST endpoint docstrings** (part of `c036ff7`, `359aa70`) — OpenAPI-style docstrings on all Flask routes.

---

## Infrastructure

- **Makefile: app / verify / release targets** (part of `c036ff7`) — `make app` builds + signs `.app` bundle; `make verify` runs full test suite + lint; `make release` chains both.
- **GitHub Actions CI workflow** (part of `01b3993`) — matrix build on `ubuntu-latest`/`macos-latest`, runs `pytest` with `PYTHONPATH` set, caches `.venv`.
- **.gitignore** (part of `a1fa1ec`) — covers Python cache, `__pycache__`, `*.pyc`, `.venv*`, `*.sock`, `*.log`, `*.ndjson`, macOS `.DS_Store`, Swift `.build/`.
- **chore: update .app bundle binary** (`7701109`, `1f6e165`) — binary refreshed after extension splits to keep `.app` bundle in sync with latest Swift build.

---

## Stats

| Metric | Before | After |
|---|---|---|
| Tests | 264 | 377+ |
| `service.py` lines | 4006 | 2924 |
| `HistoryPanelController.swift` main | 4700 | 2014 |
| Swift extension files | 0 | 8 |
| Sonnet agents used | — | 90+ |
| Commits this session | — | 28 |

---

## Known Issues

- Test failures on `mlx`, `pyannote`, `sounddevice` imports are pre-existing (dev environment lacks GPU ML deps); not introduced this session.
- `.app` bundle committed as binary — not suitable for GitHub distribution without Git LFS.
