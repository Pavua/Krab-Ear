# Wave 789: Swift LOC Audit — KrabEarAgent Source Files

**Date:** 2026-05-26  
**Branch:** feature/swift-loc-audit-W789  
**Scope:** `native/KrabEarAgent/Sources/KrabEarAgent/*.swift` — 86 files, 29,450 total LOC

---

## Top 25 Files by LOC

| Rank | File | LOC | Funcs | Status |
|------|------|-----|-------|--------|
| 1 | `HistoryPanelController.swift` | 2419 | 37 | **URGENT SPLIT** |
| 2 | `HistoryPanelController+History.swift` | 1442 | 55 | candidate |
| 3 | `HistoryPanelController+Settings.swift` | 1405 | 57 | candidate |
| 4 | `main.swift` | 1351 | 86 | candidate |
| 5 | `CallAutomationController.swift` | 1123 | 36 | candidate |
| 6 | `KrabEarTheme.swift` | 899 | 35 | monitor |
| 7 | `RealtimeOverlayController.swift` | 865 | 34 | monitor |
| 8 | `HistoryPanelController+Import.swift` | 825 | 34 | monitor |
| 9 | `DiagnosticsTabView.swift` | 734 | 27 | monitor |
| 10 | `HistoryPanelController+CallAssist.swift` | 732 | 24 | monitor |
| 11 | `HistoryPanelController+Settings+ClaudeDesign.swift` | 682 | 22 | monitor |
| 12 | `AnalyticsDashboardViewController.swift` | 680 | 20 | monitor |
| 13 | `main+StatusMenu.swift` | 643 | 10 | OK |
| 14 | `IPCClient.swift` | 508 | 11 | OK |
| 15 | `Models.swift` | 445 | — | OK |
| 16 | `main+RealtimeOverlay.swift` | 427 | 13 | OK |
| 17 | `LiveSubtitlesOverlay.swift` | 426 | — | OK |
| 18 | `HistoryPanelController+GlossarySuggestions.swift` | 417 | — | OK |
| 19 | `TranslationStreamView.swift` | 416 | — | OK |
| 20 | `SelectionTranslator.swift` | 405 | — | OK |
| 21 | `HistoryPanelController+HistoryEnhancements.swift` | 401 | — | OK |
| 22 | `HotkeyManager.swift` | 384 | — | OK |
| 23 | `main+PasteHandling.swift` | 366 | 13 | OK |
| 24 | `HistoryPanelController+SemanticSearch.swift` | 365 | — | OK |
| 25 | `HistoryPanelController+ActionItems.swift` | 365 | — | OK |

**Threshold:** >800 LOC = candidate for split; >1500 LOC = needs urgent split.

---

## Refactor Candidates

### 1. `HistoryPanelController.swift` — 2419 LOC (URGENT)

The base class itself (not counting the 25 extension files totaling ~7,300 additional LOC) has 2,419 lines. This is a severe violation of single-responsibility even by AppKit standards.

**Root cause:** `setupUI()` at line 552 is ~976 lines long — the largest single function in the entire codebase. It builds all 6 tab views inline (Dictation, Live Translation, History, Conversation, Calls, Diagnostics) without delegation to sub-methods.

**Largest functions:**

| Function | Start line | Approx size |
|----------|-----------|-------------|
| `setupUI()` | 552 | ~976 lines |
| `applyVisualTheme()` | 1770 | ~219 lines |
| `setupHistoryTab(_:)` | 1692 | ~78 lines |
| `setupLiveTranslationTab(_:)` | 1625 | ~67 lines |
| `setupDictationTab(_:)` | 1562 | ~63 lines |

**Recommended split:**
- Extract `applyVisualTheme()` → `HistoryPanelController+ApplyTheme.swift` (already partial — `+ApplyTheme+HistoryTab.swift`, `+ApplyTheme+DictationSections.swift`, `+ApplyTheme+LiveTab.swift` exist but the base `applyVisualTheme()` remains in the core file)
- Extract `setupUI()` body into per-tab setup functions → `HistoryPanelController+Setup.swift`
- The 976-line `setupUI()` is the single highest-priority refactor item in the entire Swift codebase

---

### 2. `HistoryPanelController+History.swift` — 1442 LOC (candidate)

55 functions. The `tableView(_:viewFor:)` delegate at line 1110 is ~137 lines (complex cell rendering). The `onRetranslateSelected()` action at line 244 is ~100 lines.

**Recommended split:**
- Extract `NSTableViewDataSource` / `NSTableViewDelegate` conformance → `HistoryPanelController+HistoryTable.swift`
- Keeps action handlers in `+History.swift`, table rendering separate

---

### 3. `HistoryPanelController+Settings.swift` — 1405 LOC (candidate)

57 functions. Three large section-builder functions at 182, 126, and 119 lines each (`syncSettingsControls`, `buildAudioPipelineSection`, `buildSystemSection`).

**Recommended split:**
- Extract section builders → `HistoryPanelController+Settings+Builders.swift`
- Note: `HistoryPanelController+Settings+ClaudeDesign.swift` (682 LOC) already started this pattern but was not applied to the base `+Settings.swift`

---

### 4. `main.swift` — 1351 LOC (candidate)

86 `func` occurrences — highest function density in the codebase. The `AgentAppDelegate` class has extensive `@objc` menu action handlers (lines 447–866: ~50 one-liner `@objc func` wrappers). These are menu item targets that forward to a helper.

**Largest functions:**

| Function | Start line | Approx size |
|----------|-----------|-------------|
| `completeStartupAfterBackendReady()` | 257 | ~114 lines |
| `applicationDidFinishLaunching(_:)` | 159 | ~98 lines |
| `setupUI(window:)` (QuickStartWindowController) | 1116 | ~90 lines |
| `applyHotkeyProfile(_:)` | 721 | ~41 lines |

**Already partially split** via 13 `main+*.swift` extension files (2,645 LOC across extensions). The remaining core file retains startup lifecycle + all `@objc` menu actions. Further extraction could move the `@objc` menu actions to `main+MenuActions.swift`.

---

### 5. `CallAutomationController.swift` — 1123 LOC (candidate)

36 functions. `buildUI()` at line 348 is ~203 lines — a monolithic UI builder similar in pattern to the `HistoryPanelController.setupUI()` issue.

**Largest functions:**

| Function | Start line | Approx size |
|----------|-----------|-------------|
| `buildUI()` | 348 | ~203 lines |
| `tableView(_:viewFor:)` | 1073 | ~47 lines |
| `handleDialResponse(_:phone:goal:)` | 867 | ~41 lines |

**No extension files exist** for `CallAutomationController`. Given the HistoryPanelController precedent, splitting into `+UI`, `+TableDelegate`, `+CallActions` would reduce the core file to ~500 LOC.

---

## Extensions Not Yet Split (HistoryPanelController)

The following HistoryPanelController extension files exceed 800 LOC individually and are themselves candidates for further subdivision:

| File | LOC | Largest func | Candidate for sub-split |
|------|-----|-------------|------------------------|
| `+History.swift` | 1442 | `tableView(_:viewFor:)` ~137 lines | Yes — extract table delegate |
| `+Settings.swift` | 1405 | `syncSettingsControls()` ~182 lines | Yes — extract builders |
| `+Import.swift` | 825 | complex import pipeline | Monitor |
| `+CallAssist.swift` | 732 | — | Monitor |

The `HistoryPanelController` family total (base + all extensions): **~9,750 LOC across 27 files** — the largest single logical unit in the Swift codebase.

---

## Already Well-Split

The following use the extension pattern correctly and are within healthy bounds:

- **`ConversationViewController`**: 4 files (`.swift`, `+UI`, `+WebSocket`, `+Audio`) — 243/250/205/161 LOC each. Exemplary split.
- **`main+*.swift` extensions**: 13 files (25–643 LOC each, all purpose-isolated)
- All other files at <500 LOC are healthy.

---

## Summary

| Category | Files | Total LOC |
|----------|-------|-----------|
| URGENT (>1500 LOC) | 3 | 5,212 |
| Candidate (800-1500 LOC) | 4 | 4,014 |
| Monitor (500-800 LOC) | 5 | 3,792 |
| Healthy (<500 LOC) | 74 | 16,432 |
| **Total** | **86** | **29,450** |

**Single highest-priority action:** Extract or decompose `HistoryPanelController.setupUI()` (976-line function). It is the primary driver of the 2,419-line base file and makes the class untestable in isolation. A pure extraction — moving the per-tab body into the existing `setupXxxTab()` sub-methods and then into extension files — would reduce the base file to under 800 LOC without changing any behavior.
