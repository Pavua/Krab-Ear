# Migration observation lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После закрытия/повторного открытия панели старые migration callbacks не воздействуют на новое UI-наблюдение.
**Architecture:** Один production Foundation coordinator управляет IPC completion/poll/retry/outcome/confirmation lifecycle; панель внедряет транспорт и UI эффекты. Поколение окна проверяется перед всеми эффектами; закрытие не отменяет backend migration.
**Tech Stack:** Swift 5 language mode, Foundation/XCTest/standalone swiftc, существующий pytest Python 3.12; новых dependencies нет.
**Spec:** `docs/superpowers/specs/2026-10-11-migration-observation-lifecycle.md`.
**База:** `origin/codex/krab-ear-v2=16d2b226`, stacked source `5d0ed47fa5d0602eda9ef23171b47f5e453c3407`; owned `codex/ear-migration-lifecycle-20261011`.

## Global Constraints

- Source/PR/hosted CI only; no real history/Keychain/activation/migration/purge/live IPC/SSE/runtime/neighbor edits.
- No dependency install/full local package build/process stops/global settings. Small serial swiftc/pytest checks only, full Swift build/tests remote GitHub-hosted.
- Single @MainActor coordinator owns active/epoch/polling; remove independent associated migratePolling state; capture epoch before dispatch and check before effects.
- Preserve existing IPC names/payloads, watcher-before-migrate ordering, 0.5-second retry, 1.5-second terminal fallback and HistoryMigrationOutcome semantics.
- Close invalidates first, then cleanup; activate idempotent while open, fresh epoch after close. Backend migration is never cancelled by window close.
- Keep AppKit layout/text/color unchanged. No runModal/new EventBridge/wake-word SSE. IPC stays off-main; UI/coordination on main.
- RED must fail from a real stale production effect; source-contract checks supplement executable callbacks and cannot replace them.
- Explicit git add paths only; no stash/reset/clean/foreign edits. Implementer owns only listed eight source/test/CI files; root owns docs.

## Review Focus

- Old unavailable/terminal/error callbacks must not clear polling or stop watcher belonging to reopened B.
- Already queued 0.5/1.5 callbacks must have no effect even when manually delivered despite cancellation.
- Confirmation status/sheet completions from A must not show sheet/start migration after close/reopen.
- Synchronous fake completion/repeated show must keep ordering and avoid creating duplicate poll/watch state.
- Missing/malformed IPC response must preserve existing refresh/start/poll failure behavior and not silently report completion.

### Task 1: Production observation coordinator, adapters and executable regressions

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryMigrationObservationCoordinator.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+SecuritySettings.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift`
- Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/HistoryMigrationObservationCoordinatorTests.swift`
- Modify: `native/KrabEarAgent/Tests/KrabEarAgentTests/HistoryEncryptionOutcomesTests.swift`
- Create: `KrabEar/tests/test_history_migration_observation_swift_harness.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/krabear-ci.yml`

**Interfaces:**
- Existing `HistoryMigrationOutcome.resolve` / `effectiveTerminalStatus` unchanged.
- Coordinator `activate()`, `close()`, `refresh()`, `startMigration()` and `confirmAndStartMigration()`; read-only polling for control tests if needed.
- Inject real request completion, schedule closure, start/stop/read watcher effects, render events and confirmation presenter; choose concrete typed closure aliases with Swift 5/MainActor compatibility. No test-only cleanup production methods.
- Adapter owns one coordinator via associated object; activate at start of showPanel before sync/load; cancelMigrationEventWatch delegates close from windowWillClose.
- Existing presentMigrationOutcome renders an already resolved outcome; no duplicate outcome/poll orchestration remains in panel.

- [x] Step 1: Write focused executable tests and behavior-preserving extraction seam. Mirror current success/status/error/terminal/confirmation flow; do not fix stale delivery yet. Run positive controls for watcher-before-migrate, refresh takeover, 0.5 retries, IPC-over-SSE outcomes and 1.5 fallback. Keep commands/output/snapshot patch in this plan's ledger.
- [x] Step 2: Run real stale regressions on extracted pre-fix behavior. Hold start success/unavailable/transport error, refresh status, polled terminal/error, queued 0.5/1.5 closures, confirmation count and sheet response. Close and release each callback: no request/watch/render/scheduling changes after close snapshot. Close→activate B, then release A: B polling/watch stays active, its own next status/terminal remains functional. Expected RED is extra effect or B state damage, not missing symbol/build failure. Preserve failing output before patch.
- [x] Step 3: Add minimal active/epoch gating to actual production callbacks, close-before-cleanup and idempotent activation. Also prevent a stale refresh completion inside an active observation from repainting fresh migration state. Preserve original parsing/outcome behavior and no backend cancellation.
- [x] Step 4: Same executable cases GREEN. One shared suite runs in XCTest and standalone `swiftc -swift-version 5 -parse-as-library -DHISTORY_MIGRATION_OBSERVATION_STANDALONE` with only outcomes/coordinator/test source. Python wrapper compiles/runs with bounded timeouts and Darwin/toolchain guards. Add narrow updated adapter contracts; compile/run existing outcomes tests headlessly if feasible, otherwise mark remote CI coverage. Full local Swift package build forbidden.
- [x] Step 5: Both existing Swift CI filters additionally run `HistoryMigrationObservationCoordinatorTests|HistoryEncryptionOutcomesTests|SecuritySettingsWiringContractTests|EncryptionToggleUnknownStateTests`; no runner/event/permission/dependency change. This must execute behavioral and affected contracts on hosted macOS; Linux wrapper skip alone is insufficient.
- [x] Step 6: Run existing local flake8 on new Python file; run selected Python3.12 parity with existing `/private/tmp/py312` without rebuilding/installing. If project pre_merge wrapper tries install/rebuild, stop and ask root for guarded wrapper. Save exact commands/results, no full backend locally; full suite remote CI. Run git diff --check.
- [x] Step 7: Self-review owned eight paths; explicit stage/commit with `Co-Authored-By: GPT-6.1 Sol <noreply@openai.com>`. Do not push/PR or edit root docs. Report RED/GREEN evidence, extraction fidelity, each callback and positive control, actor/weak capture assumptions, boundaries and changed files.

## Root completion gates

- [x] Task-scoped independent spec AND quality review; Astra finding fixed in c43a5a4d, scoped re-review Approved.
- [ ] Final strong lifecycle diff review; exact patch/source manifest.
- [x] NOW/ROADMAP/handoff/project memory/global extension updated as source checkpoint; final dynamic receipt in PR body/ignored ledger, #2085 preserved.
- [ ] Separate draft PR to codex/krab-ear-v2; attach artifact; all exact-head CI completed SUCCESS; no merge/deploy/live claims.
