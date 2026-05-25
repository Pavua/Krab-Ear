# main+*.swift extensions audit — Wave 177

**Date:** 2026-05-19  
**Auditor:** Claude Code (Wave 177)  
**Scope:** `native/KrabEarAgent/Sources/KrabEarAgent/main+*.swift`

---

## Files (sorted by LOC desc)

| File | LOC | Sync `ipcClient.call` | Async `callAsync` | `@objc` | Dedicated Swift test |
|------|-----|----------------------|-------------------|---------|----------------------|
| main+StatusMenu.swift | 643 | 0 | 2 | 0 | 0 |
| main+RealtimeOverlay.swift | 422 | 0 | 0 | 0 | 0 |
| main+PasteHandling.swift | 366 | 1 (offloaded) | 0 | 0 | 0 |
| main+HotkeyRecording.swift | 249 | 0 (uses callWithRecovery via Task.detached) | 0 | 0 | MainHotkeyRecordingTests.swift |
| main+Errors.swift | 187 | 0 | 0 | 0 | 0 (covered by ErrorActionHandlerTests.swift) |
| main+HealthMonitor.swift | 148 | 0 | 0 | 0 | HealthMonitorTests.swift |
| main+QuickReplace.swift | 124 | 0 (callWithRecovery on @MainActor — see risk) | 0 | 1 | 0 |
| main+PasteAppMemory.swift | 113 | 1 (recordPasteProfile — main thread) | 0 | 0 | 0 (covered by test_paste_app_memory.py Python side) |
| main+QuickPresets.swift | 106 | 0 (callWithRecovery via main thread — see risk) | 0 | 3 | 0 |
| main+LiveSubs.swift | 96 | 0 | 0 | 1 | LiveSubsTests.swift |
| main+Bookmarks.swift | 96 | 0 (callWithRecovery — see risk) | 0 | 0 | 0 |
| main+IPCRecovery.swift | 42 | 2 (utility wrapper, not @objc) | 0 | 0 | 0 |
| main+LiveSubsHotkey.swift | 25 | 0 | 0 | 0 | 0 (covered by LiveSubsTests.swift) |

**Total:** 13 files, 2617 LOC

---

## Risks identified

### MEDIUM: Sync `callWithRecovery` on main thread — `main+Bookmarks.swift` lines 41, 55

```swift
// main+Bookmarks.swift:39-58
func createBookmarkDuringRecording() {
    guard let stateData = try? callWithRecovery(method: "get_recording_state", ...) else { ... }
    guard (try? callWithRecovery(method: "add_bookmark", ...)) != nil else { ... }
```

`createBookmarkDuringRecording()` is called from `handleBookmarkHotkey()` which is annotated `@MainActor`. Both `callWithRecovery` calls are therefore synchronous on the main thread. IPC timeout is the default (not `quickTimeoutSec`). Under slow backend or socket backpressure, this is the AGENT-3 pattern. The IPC latency for `get_recording_state` and `add_bookmark` is typically <50 ms but is not guaranteed.

**Recommended fix:** Wrap in `Task.detached` (same pattern applied in main+HotkeyRecording.swift line 30).

### MEDIUM: Sync `callWithRecovery` on main thread — `main+QuickPresets.swift` line 38

```swift
// main+QuickPresets.swift:36-44
func applyRecordingPreset(_ presetId: String, source: String = "menu") {
    do {
        _ = try callWithRecovery(method: "apply_profile_preset", params: ["profile": presetId])
        // ...refreshStatusItemTitle(), rebuildStatusMenu() follow
    }
}
```

Called from `cycleToNextPreset()` which is dispatched to `DispatchQueue.main.async` from the global hotkey monitor. Sync IPC on main thread. `apply_profile_preset` triggers settings write + possible model reload — latency can exceed 200 ms.

**Recommended fix:** `Task.detached` with `@MainActor` UI-update tail.

### LOW: Sync `callWithRecovery` on main thread — `main+QuickReplace.swift` line 71

```swift
// main+QuickReplace.swift:30, 71
@objc @MainActor
func onReplaceWordRequested() {
    // ... NSAlert.runModal() — already blocking the main thread
    let result = try callWithRecovery(method: "replace_word_in_last_transcript", ...)
}
```

The comment (line 69) explicitly notes "call synchronously like QuickPresets — fast (<50 ms)". This is inside a modal alert's continuation, so the runloop is already blocked. Risk is lower but the pattern is technically still sync IPC on main. If backend is under load (e.g., transcribing), the replace call can exceed 50 ms and add noticeable lag to alert dismissal.

### LOW: Sync `ipcClient.call` on main thread — `main+PasteAppMemory.swift` line 67

```swift
// main+PasteAppMemory.swift:65-71
func recordPasteProfileForApp(bundleId: String, profile: String) {
    _ = try? ipcClient.call(
        method: "record_paste_app_profile",
        params: ["bundle_id": bundleId, "profile": profile]
    )
}
```

Called from paste handling UI path. No timeout override (uses default). Fire-and-forget semantics (`_ = try?`) reduce impact but return value is awaited synchronously. The `fetchPasteProfileForApp` companion function correctly offloads to a background `DispatchQueue.global`; `recordPasteProfileForApp` should match.

**Recommended fix:** Wrap in `DispatchQueue.global(qos: .utility).async` (same pattern as `main+PasteHandling.swift:221`).

### INFO: `callWithRecovery` itself uses sync `ipcClient.call` — `main+IPCRecovery.swift` lines 18, 23

This is intentional: `callWithRecovery` is a thin retry wrapper and is expected to be called from already-offloaded contexts. The function itself is not the problem — callers that invoke it from main thread are (see Bookmarks, QuickPresets above). The wrapper is correct and well-documented.

---

## Sync IPC call sites summary

| File | Line | Method | Thread context | Risk |
|------|------|--------|---------------|------|
| main+IPCRecovery.swift | 18, 23 | utility wrapper | Caller-determined | INFO |
| main+PasteHandling.swift | 222 | `set_paste_status` | `DispatchQueue.global(.utility)` — SAFE | SAFE |
| main+PasteAppMemory.swift | 67 | `record_paste_app_profile` | Main thread (likely) | LOW |
| main+Bookmarks.swift | 41, 55 | `get_recording_state`, `add_bookmark` | `@MainActor` | MEDIUM |
| main+QuickPresets.swift | 38 | `apply_profile_preset` | `DispatchQueue.main.async` | MEDIUM |
| main+QuickReplace.swift | 71 | `replace_word_in_last_transcript` | `@MainActor` (post-modal) | LOW |

**Total direct sync `ipcClient.call(` sites: 4**  
**Total `callWithRecovery` sites on main thread: 4 (Bookmarks×2, QuickPresets×1, QuickReplace×1)**

---

## Test coverage gaps

| File | Swift test file | Python IPC test | Gap severity |
|------|----------------|-----------------|--------------|
| main+StatusMenu.swift (643 LOC) | NONE | none | HIGH — largest file, menu-rebuild logic, async IPC untested |
| main+RealtimeOverlay.swift (422 LOC) | NONE | none | HIGH — overlay lifecycle, partial transcript display untested |
| main+PasteHandling.swift (366 LOC) | NONE | none | HIGH — core paste path with AGENT-8 regression risk |
| main+Bookmarks.swift (96 LOC) | NONE | test_bookmarks.py (backend only) | MEDIUM — Swift hotkey + sync IPC callsite untested |
| main+QuickPresets.swift (106 LOC) | NONE | test_config_presets_library.py (backend) | MEDIUM — preset cycle + sync IPC on main untested |
| main+QuickReplace.swift (124 LOC) | NONE | test_replace_word_in_transcript.py (backend) | MEDIUM — @objc modal + sync IPC untested |
| main+PasteAppMemory.swift (113 LOC) | NONE | test_paste_app_memory.py (backend) | LOW — Swift cache logic untested |
| main+IPCRecovery.swift (42 LOC) | NONE | none | LOW — simple wrapper, static helper testable |
| main+LiveSubsHotkey.swift (25 LOC) | partial (LiveSubsTests) | none | LOW — trivial |

Files **with** dedicated Swift tests: `main+HotkeyRecording.swift` (MainHotkeyRecordingTests), `main+HealthMonitor.swift` (HealthMonitorTests), `main+LiveSubs.swift` (LiveSubsTests).

---

## Recommended Wave 178 actions

### Priority 1 — Fix sync IPC on main thread (MEDIUM risks)

1. **main+Bookmarks.swift** — wrap `createBookmarkDuringRecording()` in `Task.detached`, mirror the `main+HotkeyRecording.swift` pattern (comment line 26 is the reference).
2. **main+QuickPresets.swift** — wrap `applyRecordingPreset` body in `Task.detached`; move `refreshStatusItemTitle()` and `rebuildStatusMenu()` to `@MainActor` tail.

### Priority 2 — Add Swift unit tests for top-3 untested files

3. **main+StatusMenu.swift** — add `MainStatusMenuTests.swift`: test `buildStatusMenu()` item count, `updateStatusMenuState()` transitions, async IPC calls get dispatched.
4. **main+RealtimeOverlay.swift** — add `MainRealtimeOverlayTests.swift`: test show/hide lifecycle, `@MainActor` guard, partial vs final display logic.
5. **main+PasteHandling.swift** — add `MainPasteHandlingTests.swift`: test `handleTranscriptionResult` dispatch path, `updatePasteStatus` offload to global queue.

### Priority 3 — Minor fixes

6. **main+PasteAppMemory.swift** line 67 — wrap `recordPasteProfileForApp` IPC call in `DispatchQueue.global(qos: .utility).async` (LOW risk, easy fix).
7. **main+IPCRecovery.swift** — add inline unit test for `isConnectionError` static helper (pure function, no mocks needed).
