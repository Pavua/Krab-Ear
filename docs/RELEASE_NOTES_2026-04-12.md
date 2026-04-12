# Release Notes — 2026-04-12

## Summary

GUI expansion and code quality session: 15 new IPC methods surfaced in the Swift agent UI, .app bundle created for distribution, `CallAssistService` extracted into its own module with 22 unit tests, and a round of polish fixes for layout, scroll, and dead code.

## New Features

- **GUI for 15 IPC methods** (`8d751ea`) — Swift `HistoryPanelController` gains dedicated UI controls for diagnostics, profile presets, audio device selection, clipboard history, and history management actions.
- **Live Translation tab keyboard shortcuts** (`a0e4228`) — `Cmd+D` (Dictation), `Cmd+E` (Translation), `Cmd+I` (Import) added; width constraints prevent layout breakage on narrow windows.
- **.app bundle** (`b462eec`, `7701109`) — `Krab Ear.app` with `Info.plist`, `LSUIElement` (menu-bar-only mode), and `codesign -s -` sign. Binary updated to latest build.

## Bug Fixes

- **History tab scroll** (`5d26fb3`) — scroll view restored; toolbar layout corrected for narrow window widths.
- **Dictation + Live Translation tab scrolling** (`b7b4dbd`) — scroll wrappers added so all content is reachable.
- **GUI polish** (`dfa6f2f`) — uniform button styling; `settingsBar` section width constraints applied consistently.
- **Code quality** (`1534dc1`) — removed duplicate function in `engine.py`, deleted unused import in `rest_server.py`, eliminated empty f-strings and dead variable in `service.py`.

## Improvements

- **`CallAssistService` extraction** (`72163c9`) — ~1 013-line module extracted from `backend/service.py` into `backend/call_assist_service.py`. 22 new unit tests added (`test_call_assist_service.py`), raising the total to 286 tests.
- **CLAUDE.md expansion** (`bf3a738`) — 11 new architectural patterns documented: `.app` bundle, `LSUIElement`, `iCloud errno 11` workaround, transcript `.md` generation, `LaunchAgent` socket path, LLM length-ratio guard, `CollapsibleSectionView` persistence, event contract envelope, config env-var override, test path setup, and legacy `AudioEngine` static aliases.

## Technical Details

- Tests: 286 total (274 pass, 4 skip, 5 failures on import-level deps, 3 collection errors — pre-existing)
- Commits this session: 10
- Files changed: 10 (+2 007 insertions, −49 deletions)
- Key files: `KrabEar/backend/call_assist_service.py` (new, 1 013 lines), `native/KrabEarAgent/HistoryPanelController.swift` (+556 lines), `KrabEar/tests/test_call_assist_service.py` (new, 258 lines), `Krab Ear.app/` bundle (new)

## Known Issues

- 5 test failures and 3 collection errors are pre-existing (import-level dependency issues with `mlx`, `pyannote`, `sounddevice` not installed in test environment); not introduced this session.
- `.app` bundle is committed as a binary in the repo — not suitable for distribution via GitHub without Git LFS.
