# Audio ducking lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** убрать блокировку MainActor при управлении звуком и сохранить исходный snapshot до доказанного восстановления.

**Architecture:** один сервис владеет snapshot и UUID lease на отдельной serial queue. Абсолютный monotonic deadline включает очередь и команды. Owned child имеет единственного владельца waitpid; неубранный child блокирует новые изменения. MainActor ожидает prepare перед IPC и ставит restore в очередь синхронно без ожидания процесса.

**Tech Stack:** Swift5 mode/SwiftPM6, macOS13+, Foundation/Dispatch/Darwin, XCTest; новых dependencies нет.

**Spec:** [Контракт и границы](../specs/2026-10-16-audio-ducking-lifecycle-design.md).

**База:** origin/codex/krab-ear-v2 = be3900730aaad64974ec3b70de4b9b90fc511d5c.
Worktree: `/Users/pablito/.codex/worktrees/ear-status-idle-20261016/Krab Ear`, branch `codex/ear-audio-ducking-lifecycle-20261016`.

## Global Constraints

- Общий canonical checkout с чужим WIP не менять, git add только явными путями.
- Не запускать второй production agent; unit tests не вызывают app launch.
- Прод-backend только через safe_backend_restart.command после release gate.
- OrbStack, Main/VG source, settings и lifecycle не менять.
- Нет CoreAudio migration, TCC changes, secrets, real audio/notifications из unit tests.
- Не включать REST_IN_PROCESS_ENABLED; не мержить #1875; не возвращать wake word на SSE; не строить второй EventBridge.
- Не запускать brain/model downloads/installs. Bash3.2 и BSD compatibility сохраняется.
- Swift visual redesign вне scope; mute-before-recording и mic effective100 сохраняются.
- Внешние звонки и real encryption/migration/purge/rotation вне scope.
- Runtime/mic/autopaste acceptance и историческая причинность AppHang/504 — отдельные утверждения.

## Review Focus

- Failed/partial mutation и failed restore сохраняют именно исходный snapshot.
- Late child не выполняет write после успешного restore; cleanup uncertainty блокирует новый child.
- Cancel/stop до start IPC не открывает recorder; после IPC остаются CAS/reconciliation.
- PreparingAudio не является отправленным IPC для meeting promotion.
- Ранний meeting finish и stale finish не освобождают lease другого поколения.

### Task 1: Actual-class RED, затем serialized service и bounded child

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/SystemAudioDuckingService.swift`
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/AudioDuckingCommandRunner.swift`
- Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/AudioDuckingLifecycleWiringTests.swift`

**Interfaces:** `AudioDuckingLease(id:UUID)`, `prepare(lease:enabled:duckPercent:) async`, `requestRestore(lease:completion:)` (синхронная постановка в очередь), `AudioDuckingCommandRunning.run(script:deadline:)`. Results: ready/bypassed/failed и restored/absentOrStale/retained. Protocol и данные Sendable; mutable state доступен только serial queue.

- [x] Step 1: добавить constructor injection без изменения production default и три воспроизводящих теста настоящего класса. Baseline test body для lost restore:

```swift
var scripts: [String] = []
var replies = [true, false, true]
let service = SystemAudioDuckingService(
    runScript: { script in scripts.append(script); return replies.removeFirst() },
    captureScript: { $0.contains("output muted") ? "false" : "70" }
)
service.duckForRecording(enabled: true, duckPercent: 100)
service.restoreAfterRecording()
service.restoreAfterRecording()
XCTAssertEqual(scripts.count, 3)
XCTAssertEqual(scripts.last, "set volume output volume 70\nset volume output muted false")
```

Соседние RED: mutating runner вернул false, но restore обязан выполниться; задержанный runner200ms не должен задержать MainActor heartbeat до200ms.

- [x] Step 2: `cd native/KrabEarAgent && swift test -j 2 --filter AudioDuckingLifecycleWiringTests`; сохранить RED по count2 вместо3 / отсутствию restore / heartbeat latency, не по compile error.
- [x] Step 3: реализовать полный queue-owned lifecycle. Snapshot публикуется до mutating command; state active/restorePending/cleanupUnconfirmed различаются. Failed restore не очищает snapshot. Новый prepare может один раз восстановить restorePending, но не чужой active lease. Disabled не запускает child. Unchanged already-muted не меняет звук. Enabled preparation failure отказывает start.
- [x] Step 4: runner использует posix_spawn + bounded nonblocking stdout + stderr null, pinned unreaped PID и единственный waitpid. Deadline перед spawn и в polling loop; timeout → owned kill/reap с отдельным cleanup budget. Если cleanup не подтверждён — quarantine без следующих commands. Никаких kill-by-name/чужих PID.
- [x] Step 5: обновить тесты на окончательный async API, сохранить три behavior assertions. Добавить FIFO restoreG1→prepareG2, stale restoreG1, retry failed restore, disabled/already-muted/read failure, partial mutation, queue expiry. Harmless child tests: deadline/reap, output cap, новый child не убивается старым timer.
- [x] Step 6: повторить тот же targeted filter; expected PASS. Удалить временные synchronous compatibility APIs, когда Task2 переведёт callers.

### Task 2: Actual start/promotion callbacks и release gate

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+HotkeyRecording.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+MeetingPanel.swift`
- Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/AudioDuckingStartWiringTests.swift`
- Modify: `docs/NOW.md`

- [x] Step 1: inject actual service и NotificationService(no-op runner) в delegate constructor с production defaults. В тесте delayed runner удерживает prepare; настоящий performRecordToggle устанавливает stop-intent; после release fake runner число start_recording =0. Добавить success порядок mute→IPC, cancellation и meeting toggle во время preparingAudio без meeting_start. Expected baseline RED: IPC уходит после отмены либо heartbeat блокируется.
- [x] Step 2: добавить preparingAudio/requestSent, UUID lease до await и проверку после await:

```swift
guard !Task.isCancelled,
      !recordingStopRequestedDuringStart,
      recordingStartRequestID == startRequestID,
      dictationDuckingLease == lease else {
    releaseDictationDucking(expectedLease: lease)
    return
}
```

Gate удерживается до выхода; phase=requestSent ставится непосредственно перед start_recording. Preparation failure обрабатывается отдельно от transport ambiguity. requestRestore ставится в очередь до возврата release helper; очистка delegate binding не ждёт MainActor Task.

- [x] Step 3: все шесть restore sites используют ожидаемый lease. Ambiguous/nonterminal recovery сохраняет lease. Promotion хранит binding(startRequestID,lease,generationToken), передаёт его только через нынешние token/revision guards. Finish-before-start оставляет terminal marker до подтверждения; matching start/reconcile разрешает release. Terminal finish получает lastDeliveredGenerationToken, stale token не освобождает новый lease. Full inFlight остаётся в policy; отдельный requestMayBeActive гейтит только deferUntilStart.
- [x] Step 4: actual callback regressions: ordinary handoff, early promotion, ambiguous reconcile, finish-before-start-response, stale finishG1/G2. Ни реального audio/paste, ни app launch. Классы …WiringTests попадают в существующий hosted filter.
- [x] Step 5: `swift test -j 2 --filter 'AudioDucking.*WiringTests|MainHotkeyRecordingTests|Meeting.*Tests|SystemAudioDuckingServiceTests'`, затем `swift build -c release -j 2`. Запуск только в согласованном compile lease; build не запускает app.
- [x] Step 6: найти зависимые Python source-contract tests по SystemAudioDuckingService/duckForRecording/restoreAfterRecording и прогнать только их с PYTHONPATH и existing venv. `make audit-all`; no runModal, diff-check. Actual Swift tests — главный gate, copied Testable не доказательство.
- [ ] Step 7: независимый Astra Ultra whole-diff gate; поправить findings до push. Git add только названные пути и оба design/plan docs. Commit, non-draft PR, attach artifact. Exact-head все CI SUCCESS/SKIPPED → merge; exact post-merge CI → runtime qualification и согласованная выкладка. Installed SHA/PID и mic/autopaste acceptance фиксировать отдельно.
