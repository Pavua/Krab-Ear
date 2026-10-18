import AppKit
import Darwin
import XCTest
@testable import KrabEarAgent

private final class InputRunner: AudioDuckingCommandRunning, @unchecked Sendable {
    let entered = DispatchSemaphore(value: 0), finish = DispatchSemaphore(value: 0)
    var cleanup = false, calls = 0, succeeds = false
    func hasEntered() -> Bool { entered.wait(timeout: .now()) == .success }
    func run(script: String, deadline: ContinuousClock.Instant) -> AudioDuckingCommandResult {
        calls += 1; entered.signal(); finish.wait(); return succeeds ? .success("") : .failure(.commandFailed, cleanupConfirmed: cleanup)
    }
}
@MainActor private final class InputFixture {
    let board: NSPasteboard, defaults: UserDefaults, queue: InputAutomationQueue, name: String
    var held = false, alive = true, pid: pid_t = 101
    var role: String? = "AXTextArea", editable: Bool? = true
    var collapses = 0
    var trustedOverride: Bool?
    var events: [String] = []
    var failAfterDeletePairs: Int?, deletePairs = 0
    var onDown: ((CGKeyCode) -> Void)?, onUp: ((CGKeyCode) -> Void)?, onModifier: (() -> Void)?
    init(capacity: Int = 8) {
        let name = "InputFixture.\(UUID().uuidString)"; self.name = name
        board = NSPasteboard(name: .init(name)); defaults = UserDefaults(suiteName: name)!
        queue = InputAutomationQueue(capacity: capacity)
        board.clearContents(); board.setString("original", forType: .string)
    }
    func service(runner: any AudioDuckingCommandRunning = SystemAudioDuckingCommandRunner(), trusted: Bool = true, budget: Duration = .seconds(5)) -> PasteService {
        PasteService(pasteboard: board, defaults: defaults,
            modifierKeysDown: { self.onModifier?(); return self.held }, accessibilityTrusted: { self.trustedOverride ?? trusted },
            keyEventFactory: { pid, key, _ in
                XCTAssertEqual(pid, self.pid)
                if key == 51, let limit = self.failAfterDeletePairs, self.deletePairs >= limit { return nil }
                return PasteKeyEventPair(down: { self.events.append("d\(key)"); self.onDown?(key) },
                                         up: { self.events.append("u\(key)"); if key == 51 { self.deletePairs += 1 }; self.onUp?(key) })
            }, inputQueue: queue, frontmostPID: { self.pid }, targetIsAlive: { _ in self.alive },
            focusRole: { _ in self.role }, focusEditable: { _ in self.editable }, collapseSelection: { _ in self.collapses += 1 },
            runner: runner, operationBudget: budget)
    }
    func close() { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
}
@MainActor final class InputAutomationIntegrationTests: XCTestCase {
    func testQueueFullAndDeadlineRefuseBeforeEffects() async {
        let f = InputFixture(capacity: 0); defer { f.close() }
        guard case .success(let lease) = await f.queue.acquire(deadline: .now + .seconds(1)) else { return XCTFail() }
        let result = await f.service().pasteToFrontmostApp("new", targetPID: 101)
        XCTAssertEqual(result.reason, "input_busy"); XCTAssertEqual(result.effect, .noEffects)
        XCTAssertTrue(f.events.isEmpty); XCTAssertEqual(f.board.string(forType: .string), "original")
        f.queue.release(lease)
        let timed = InputFixture(); defer { timed.close() }
        guard case .success(let held) = await timed.queue.acquire(deadline: .now + .seconds(1)) else { return XCTFail() }
        let expired = await timed.service(budget: .milliseconds(25)).pasteToFrontmostApp("new", targetPID: 101)
        XCTAssertEqual(expired.reason, "input_deadlineExceeded"); XCTAssertEqual(expired.effect, .noEffects)
        XCTAssertTrue(timed.events.isEmpty); timed.queue.release(held)
    }
    func testCancellationBeforeLeaseHasNoEffects() async {
        let f = InputFixture(); defer { f.close() }
        guard case .success(let lease) = await f.queue.acquire(deadline: .now + .seconds(1)) else { return XCTFail() }
        let service = f.service(), operation = Task { await f.service().pasteToFrontmostApp("new", targetPID: 101) }
        _ = service; operation.cancel()
        let result = await operation.value
        XCTAssertEqual(result.effect, .noEffects); XCTAssertTrue(f.events.isEmpty); f.queue.release(lease)
    }
    func testUserClipboardDriftDuringHeldModifierPreventsPaste() async {
        let f = InputFixture(); defer { f.close() }; f.held = true; var changed = false
        f.onModifier = { if !changed { changed = true; Task { @MainActor in
            f.board.clearContents(); f.board.setString("user copy", forType: .string); f.held = false
        } } }
        let result = await f.service().pasteToFrontmostApp("dictation", targetPID: 101)
        XCTAssertEqual(result.reason, "clipboard_changed"); XCTAssertEqual(result.effect, .noEffects)
        XCTAssertTrue(f.events.isEmpty); XCTAssertEqual(f.board.string(forType: .string), "user copy")
    }
    func testOwnExplicitCopyInvalidatesPendingPaste() async {
        let f = InputFixture(); defer { f.close() }; f.held = true; let service = f.service(); var changed = false
        f.onModifier = { if !changed { changed = true; Task { @MainActor in service.putToClipboardUserInitiated("explicit"); f.held = false } } }
        let result = await service.pasteToFrontmostApp("dictation", targetPID: 101)
        XCTAssertEqual(result.reason, "clipboard_changed"); XCTAssertTrue(f.events.isEmpty)
        XCTAssertEqual(f.board.string(forType: .string), "explicit")
    }
    func testConcealedAndTargetDriftBeforeEffects() async {
        for concealed in [false, true] {
            let f = InputFixture(); f.held = true; var changed = false
            f.onModifier = { if !changed { changed = true; Task { @MainActor in
                if concealed { f.board.declareTypes([.string, .init("org.nspasteboard.ConcealedType")], owner: nil) } else { f.pid = 202 }
                f.held = false
            } } }
            let result = await f.service().pasteToFrontmostApp("new", targetPID: 101)
            XCTAssertFalse(result.ok); XCTAssertEqual(result.effect, .noEffects); XCTAssertTrue(f.events.isEmpty); f.close()
        }
    }
    func testKeyUpAfterCancellationReportsExactPartialPairsWithoutInsert() async {
        let f = InputFixture(); defer { f.close() }; let service = f.service()
        var operation: Task<PasteAttemptResult, Never>?, ups = 0
        f.onDown = { _ in if ups == 1 { operation?.cancel() } }; f.onUp = { _ in ups += 1 }
        operation = Task { await service.revise(deleteCount: 5, text: "replacement", targetPID: 101) }
        let result = await operation!.value
        XCTAssertEqual(result.effect, .partial); XCTAssertEqual(result.backspacePairs, 2)
        XCTAssertEqual(f.events, ["d51", "u51", "d51", "u51"]); XCTAssertEqual(f.board.string(forType: .string), "original")
    }
    func testQueueWaitConsumesRevisionBudgetBeforeFirstDown() async {
        let f = InputFixture(); defer { f.close() }
        guard case .success(let held) = await f.queue.acquire(deadline: .now + .seconds(6)) else { return XCTFail() }
        let service = f.service()
        let operation = Task { await service.revise(deleteCount: 10, text: "replacement", targetPID: 101) }
        await Task.yield()
        try? await Task.sleep(for: .milliseconds(3900))
        f.queue.release(held)
        let result = await operation.value
        XCTAssertEqual(result.effect, .noEffects)
        XCTAssertTrue(["input_insufficient_budget", "input_deadlineExceeded"].contains(result.reason))
        XCTAssertTrue(f.events.isEmpty); XCTAssertEqual(f.board.string(forType: .string), "original")
    }
    func testCancelAfterPasteDownStillSendsUpAndSkipsCollapse() async {
        let f = InputFixture(); defer { f.close() }
        var operation: Task<PasteAttemptResult, Never>?
        f.onDown = { _ in operation?.cancel(); f.pid = 202 }
        let service = f.service()
        operation = Task { await service.pasteToFrontmostApp("text", targetPID: 101) }
        let result = await operation!.value
        XCTAssertEqual(result.effect, .unknown, "Отправленная пара не подтверждает результат после смены состояния")
        XCTAssertEqual(f.events, ["d9", "u9"]); XCTAssertEqual(f.collapses, 0)
    }
    func testCGPasteAfterDownDriftIsUnknownWithoutReplayRecordOrFullFallback() async {
        for drift in ["copy", "target", "secure", "cancel", "deadline"] {
            let f = InputFixture(); defer { f.close() }
            let service = f.service(budget: .milliseconds(500))
            var operation: Task<PasteAttemptResult, Never>?
            f.onDown = { key in
                guard key == 9 else { return }
                switch drift {
                case "copy": f.board.clearContents(); f.board.setString("user copy", forType: .string)
                case "target": f.pid = 202
                case "secure": f.role = "AXSecureTextField"
                case "cancel": operation?.cancel()
                default:
                    // Только fake OS leaf: bounded latency пересекает общий deadline
                    // после down. Это не проверка responsiveness реального OS вызова.
                    Thread.sleep(forTimeInterval: 0.55)
                }
            }
            let count = drift == "deadline" ? 0 : 2
            operation = Task { count > 0
                ? await service.revise(deleteCount: count, text: "replacement", targetPID: 101)
                : await service.pasteToFrontmostApp("replacement", targetPID: 101) }
            let result = await operation!.value
            XCTAssertEqual(result.effect, .unknown, drift); XCTAssertFalse(result.ok, drift)
            XCTAssertEqual(result.backspacePairs, count, drift)
            XCTAssertEqual(f.events, Array(repeating: ["d51", "u51"], count: count).flatMap { $0 } + ["d9", "u9"], drift)
            XCTAssertEqual(f.collapses, 0, drift); XCTAssertNil(service.lastPastedText, drift)
            let before = f.events, clipboard = f.board.string(forType: .string)
            _ = NSApplication.shared
            let notifications = InputNotificationSpy()
            let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
                userDefaults: f.defaults, notificationService: NotificationService(processRunner: notifications), pasteService: service)
            delegate.performAutoPaste(text: "full fallback", historyId: nil, target: nil, streamingOutcome: result)
            XCTAssertTrue(notifications.scripts.contains { $0.contains("частично или с неизвестным результатом") })
            XCTAssertEqual(f.events, before, drift); XCTAssertEqual(f.board.string(forType: .string), clipboard, drift)
        }
    }
    func testUnknownInjectedFocusDoesNotFallThroughToSystemAX() async {
        let f = InputFixture(); defer { f.close() }; f.role = nil; f.editable = nil
        let result = await f.service().pasteToFrontmostApp("text", targetPID: 101)
        XCTAssertEqual(result.effect, .completed)
        XCTAssertEqual(f.events, ["d9", "u9"]); XCTAssertEqual(f.collapses, 0)
    }
    func testStreamingUsesEachChunkTargetAndRefusesCrossTargetRevision() async {
        let f = InputFixture(); defer { f.close() }
        let controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service()))
        controller.debounceIntervalSec = 0
        await controller.handlePartial("hello ")
        await controller.handlePartial("hello there ")
        f.pid = 202
        await controller.handlePartial("hello there world ")
        XCTAssertEqual(f.events, ["d9", "u9", "d9", "u9"])
        let before = f.events
        let final = await controller.handleFinal("different")
        XCTAssertEqual(final.effect, .partial); XCTAssertEqual(f.events, before)
        XCTAssertEqual(f.board.string(forType: .string), "there ")
    }
    func testQuickEditAAfterRecordingBUsesCapturedCompletionTargetAndText() async {
        let f = InputFixture(); defer { f.close() }; _ = NSApplication.shared
        let presenter = CapturedQuickEdit(), service = f.service()
        let appA = NSRunningApplication.current
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: NoInputNotifications()),
            pasteService: service, pasteTargetActivator: PasteTargetActivator(frontmostPID: { 101 }),
            quickEditOverlay: presenter, pasteActivationTarget: { app in
                XCTAssertEqual(app.processIdentifier, appA.processIdentifier)
                return PasteActivationTarget(pid: 101, isAlive: { true }, requestActivation: { XCTFail(); return false })
            }, pasteFrontmostApplication: { nil }, playPasteCompletionSound: {})
        delegate.settings.quickEditEnabled = true; delegate.settings.autoPaste = true
        delegate.settings.clipboardMode = "never_copy"
        delegate.continueTranscriptionResult(cleanText: "recording A", historyId: nil, pasteTarget: appA)
        XCTAssertEqual(presenter.completions.count, 1)
        // B заменяет общие recording-поля, пока completion A хранится у UI leaf.
        delegate.recordingTargetApp = nil
        delegate.continueTranscriptionResult(cleanText: "recording B", historyId: nil, pasteTarget: nil)
        XCTAssertEqual(presenter.completions.count, 2)
        presenter.completions[0](.paste("edited A"))
        let end = ContinuousClock.now + .seconds(1)
        while f.events.count < 2 && ContinuousClock.now < end { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(f.events, ["d9", "u9"]); XCTAssertEqual(f.board.string(forType: .string), "edited A")
    }
    func testSnapshotEntryCapturesActivationTargetBeforeTaskRuns() async {
        let f = InputFixture(); defer { f.close() }; _ = NSApplication.shared
        var chosenPID: pid_t = 101, captures: [pid_t] = []
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: NoInputNotifications()),
            pasteService: f.service(), pasteTargetActivator: PasteTargetActivator(frontmostPID: { 101 }),
            pasteActivationTarget: { _ in
                captures.append(chosenPID)
                return PasteActivationTarget(pid: chosenPID, isAlive: { true }, requestActivation: { XCTFail(); return false })
            }, pasteFrontmostApplication: { nil }, playPasteCompletionSound: {})
        delegate.recordingTargetApp = NSRunningApplication.current
        delegate.pasteSnapshotText(text: "snapshot A", historyId: nil, sourceTag: "fixture")
        chosenPID = 202; delegate.recordingTargetApp = nil
        let end = ContinuousClock.now + .seconds(1)
        while f.events.count < 2 && ContinuousClock.now < end { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(captures, [101]); XCTAssertEqual(f.events, ["d9", "u9"])
        XCTAssertEqual(f.board.string(forType: .string), "snapshot A")
    }
    func testExplicitCopyWhileDelegateWaitsRefusesBeforeActivation() async {
        let f = InputFixture(); defer { f.close() }; _ = NSApplication.shared
        guard case .success(let lease) = await f.queue.acquire(deadline: .now + .seconds(2)) else { return XCTFail() }
        let service = f.service()
        var activations = 0
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: NoInputNotifications()),
            pasteService: service, pasteTargetActivator: PasteTargetActivator(frontmostPID: { 202 }))
        let target = PasteActivationTarget(pid: 101, isAlive: { true }, requestActivation: { activations += 1; return false })
        let operation = Task { await delegate.pasteToActivatedTarget("old dictation", target: target) }
        let admissionEnd = ContinuousClock.now + .seconds(1)
        while f.queue.pendingCount == 0 && ContinuousClock.now < admissionEnd { await Task.yield() }
        XCTAssertEqual(f.queue.pendingCount, 1)
        service.putToClipboardUserInitiated("explicit user copy")
        f.queue.release(lease)
        let result = await operation.value
        XCTAssertEqual(activations, 0, "Инвалидированный snapshot не должен активировать окно после queue wait")
        XCTAssertEqual(result.effect, .noEffects); XCTAssertTrue(f.events.isEmpty)
        XCTAssertEqual(f.board.string(forType: .string), "explicit user copy")
    }
    func testPublicAutopasteRefusalDoesNotCopyOverNewUserClipboard() async {
        let f = InputFixture(); defer { f.close() }; _ = NSApplication.shared
        guard case .success(let lease) = await f.queue.acquire(deadline: .now + .seconds(2)) else { return XCTFail() }
        let service = f.service(); var activations = 0, completed = false
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: NoInputNotifications()),
            pasteService: service, pasteTargetActivator: PasteTargetActivator(frontmostPID: { 202 }),
            pasteActivationTarget: { _ in PasteActivationTarget(pid: 101, isAlive: { true }, requestActivation: { activations += 1; return false }) },
            pasteFrontmostApplication: { nil }, playPasteCompletionSound: { completed = true })
        delegate.settings.clipboardMode = "on_failure"; delegate.settings.mode = "menubar"
        delegate.performAutoPaste(text: "old dictation", historyId: nil, target: NSRunningApplication.current)
        let admissionEnd = ContinuousClock.now + .seconds(1)
        while f.queue.pendingCount == 0 && ContinuousClock.now < admissionEnd { await Task.yield() }
        XCTAssertEqual(f.queue.pendingCount, 1)
        f.board.clearContents(); f.board.setString("new user copy", forType: .string)
        f.queue.release(lease)
        let end = ContinuousClock.now + .seconds(1)
        while !completed && ContinuousClock.now < end { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(completed); XCTAssertEqual(activations, 0); XCTAssertTrue(f.events.isEmpty)
        XCTAssertEqual(f.board.string(forType: .string), "new user copy", "Public failure handler не вправе копировать stale dictation после invalidation")
    }
    func testAlwaysCopyAutopasteDoesNotWriteBeforeQuickEditAccepts() {
        verifyAlwaysCopy(autoPaste: true)
    }
    func testAlwaysCopyClipboardOnlyModeStillCopies() {
        verifyAlwaysCopy(autoPaste: false)
    }
    private func verifyAlwaysCopy(autoPaste: Bool) {
        let f = InputFixture(); defer { f.close() }; _ = NSApplication.shared
        let presenter = CapturedQuickEdit()
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: NoInputNotifications()),
            pasteService: f.service(), quickEditOverlay: presenter,
            pasteFrontmostApplication: { nil }, playPasteCompletionSound: {})
        delegate.settings.clipboardMode = "always_copy"; delegate.settings.autoPaste = autoPaste
        delegate.settings.quickEditEnabled = true; delegate.settings.mode = "menubar"
        delegate.continueTranscriptionResult(cleanText: "dictation", historyId: nil, pasteTarget: NSRunningApplication.current)
        XCTAssertEqual(f.board.string(forType: .string), autoPaste ? "original" : "dictation")
        XCTAssertEqual(presenter.completions.count, autoPaste ? 1 : 0); XCTAssertTrue(f.events.isEmpty)
    }
    func testRunnerSuccessAfterStateDriftIsUnknownWithoutSecondChannelOrReplayRecord() async {
        for drift in ["copy", "target", "cancel", "secure", "deadline"] {
            let f = InputFixture(); defer { f.close() }
            let runner = InputRunner(); runner.succeeds = true
            let service = f.service(runner: runner, trusted: false, budget: .milliseconds(350))
            let count = drift == "deadline" ? 0 : 2
            let operation = Task { count > 0
                ? await service.revise(deleteCount: count, text: "replacement", targetPID: 101)
                : await service.pasteToFrontmostApp("replacement", targetPID: 101) }
            let enteredEnd = ContinuousClock.now + .seconds(1)
            var entered = runner.hasEntered()
            while !entered && ContinuousClock.now < enteredEnd { await Task.yield(); entered = runner.hasEntered() }
            guard entered else { runner.finish.signal(); _ = await operation.value; return XCTFail("Runner не стартовал") }
            switch drift {
            case "copy": f.board.clearContents(); f.board.setString("user copy", forType: .string)
            case "target": f.pid = 202
            case "cancel": operation.cancel()
            case "secure": f.trustedOverride = true; f.role = "AXSecureTextField"
            default: try? await Task.sleep(for: .milliseconds(250))
            }
            runner.finish.signal()
            let result = await operation.value
            XCTAssertEqual(result.effect, .unknown, drift); XCTAssertFalse(result.ok, drift)
            XCTAssertEqual(result.backspacePairs, count, "Успешный script подтвердил пары, но состояние после await неизвестно")
            XCTAssertNil(service.lastPastedText); XCTAssertEqual(runner.calls, 1); XCTAssertTrue(f.events.isEmpty)
        }
    }
    func testOverflowAndInvalidCountsDoNotSendInput() async {
        let f = InputFixture(); defer { f.close() }; let service = f.service()
        XCTAssertNil(PasteService.revisionBudget(count: Int.max))
        let overflow = await service.revise(deleteCount: Int.max, text: "new", targetPID: 101)
        let invalid = await service.revise(deleteCount: -1, text: "new", targetPID: 101)
        XCTAssertEqual(overflow.effect, .noEffects); XCTAssertEqual(invalid.effect, .noEffects); XCTAssertTrue(f.events.isEmpty)
    }
    func testCancelledCallerRetainsLeaseUntilRunnerCleanupAndUnknownHasNoSecondChannel() async {
        let f = InputFixture(); defer { f.close() }; let runner = InputRunner()
        let service = f.service(runner: runner, trusted: false)
        let operation = Task { await service.pasteToFrontmostApp("new", targetPID: 101) }
        while !runner.hasEntered() { await Task.yield() }
        operation.cancel()
        let pending = await f.queue.acquire(deadline: .now + .milliseconds(25))
        if case .failure(.deadlineExceeded) = pending {} else { XCTFail("Runner обязан удерживать lease") }
        runner.finish.signal(); let result = await operation.value
        XCTAssertEqual(result.effect, .unknown); XCTAssertEqual(runner.calls, 1); XCTAssertTrue(f.events.isEmpty)
        let refused = await service.pasteToFrontmostApp("next", targetPID: 101)
        XCTAssertEqual(refused.reason, "input_quarantined"); XCTAssertEqual(runner.calls, 1)
    }
    func testRevisionAndQueuedPasteDoNotInterleaveAndBothComplete() async {
        let f = InputFixture(); defer { f.close() }; let service = f.service()
        var queued: Task<PasteAttemptResult, Never>?
        f.onDown = { key in if key == 51 && queued == nil { queued = Task { await service.pasteToFrontmostApp("second", targetPID: 101) } } }
        let revision = await service.revise(deleteCount: 2, text: "revision", targetPID: 101)
        let second = await queued?.value
        XCTAssertTrue(revision.ok); XCTAssertEqual(revision.backspacePairs, 2)
        XCTAssertEqual(second?.effect, .completed)
        XCTAssertEqual(f.events, ["d51", "u51", "d51", "u51", "d9", "u9", "d9", "u9"])
        XCTAssertEqual(f.board.string(forType: .string), "second")
    }

    func testUserGapIsNotBridgedByLaterOwnedWrite() async {
        let f = InputFixture(); defer { f.close() }; let service = f.service()
        let old = service.captureInput(pid: 101)
        f.board.clearContents(); f.board.setString("user", forType: .string)
        let own = await service.pasteToFrontmostApp("own", targetPID: 101)
        XCTAssertTrue(own.ok)
        guard case .success(let lease) = await f.queue.acquire(deadline: .now + .seconds(1)) else { return XCTFail() }
        defer { f.queue.release(lease) }
        let result = await service.pasteToFrontmostApp("stale", targetPID: 101, lease: lease, snapshot: old)
        XCTAssertEqual(result.reason, "clipboard_changed"); XCTAssertEqual(result.effect, .noEffects)
        XCTAssertEqual(f.board.string(forType: .string), "own")
    }
    func testOldFinalBarrierDoesNotMutateNewStreamingGeneration() async {
        let f = InputFixture(); defer { f.close() }; f.held = true
        let controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service()))
        let final = controller.finalBarrier("first")
        var entered = false; f.onModifier = { entered = true }
        let deadline = ContinuousClock.now + .seconds(1)
        while !entered && ContinuousClock.now < deadline { await Task.yield() }
        XCTAssertTrue(entered)
        controller.beginSession()
        f.held = false
        let first = await final.value
        XCTAssertEqual(first.effect, .completed)
        XCTAssertFalse(controller.didStreamThisRecording, "G1 completion не изменяет G2")
        let second = await controller.handleFinal("second")
        XCTAssertEqual(second.effect, .completed)
        XCTAssertEqual(f.board.string(forType: .string), "second")
    }
    func testConcurrentZeroCapacityAdmitsOnlyOne() async {
        let queue = InputAutomationQueue(capacity: 0)
        let results = await withTaskGroup(of: InputAutomationFailure?.self) { group in
            for _ in 0..<64 {
                group.addTask {
                    switch await queue.acquire(deadline: .now + .milliseconds(50)) {
                    case .failure(let why): return why
                    case .success(let lease):
                        try? await Task.sleep(for: .milliseconds(100)); queue.release(lease); return nil
                    }
                }
            }
            var values: [InputAutomationFailure?] = []
            for await value in group { values.append(value) }
            return values
        }
        XCTAssertEqual(results.filter { $0 == nil }.count, 1)
        XCTAssertEqual(results.filter { $0 == .busy }.count, 63)
    }

    func testFinalAcceptedBehindPartialCannotRefreshClipboardSnapshot() async {
        let f = InputFixture(); defer { f.close() }; f.held = true
        let service = f.service(), controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service()))
        await controller.handlePartial("hello ")
        let partial = Task { await controller.handlePartial("hello there ") }
        // Дать partial войти в actual modifier wait; final принимается до copy.
        try? await Task.sleep(for: .milliseconds(30))
        let final = controller.finalBarrier("authoritative")
        service.putToClipboardUserInitiated("new user copy"); f.held = false
        await partial.value
        let result = await final.value
        XCTAssertEqual(result.effect, .noEffects); XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "clipboard_changed")
        XCTAssertTrue(f.events.isEmpty); XCTAssertEqual(f.board.string(forType: .string), "new user copy")
    }
    func testCancelledStreamingCallerReleasesReservedTicketBeforePriorLease() async {
        let f = InputFixture(); defer { f.close() }
        guard case .success(let held) = await f.queue.acquire(deadline: .now + .seconds(2)) else { return XCTFail() }
        let controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service()))
        await controller.handlePartial("hello ")
        let request = Task { await controller.handlePartial("hello there ") }
        let end = ContinuousClock.now + .seconds(1)
        while f.queue.pendingCount == 0 && ContinuousClock.now < end { await Task.yield() }
        request.cancel()
        try? await Task.sleep(for: .milliseconds(30))
        XCTAssertEqual(f.queue.pendingCount, 0, "Caller cancellation должна снять reserved ticket")
        f.queue.release(held); await request.value
        XCTAssertTrue(f.events.isEmpty)
    }
    func testAcceptedFinalDeadlineIncludesTailWaitAndPriorEffectsSurviveAdmissionFailure() async {
        let f = InputFixture(); defer { f.close() }
        guard case .success(let held) = await f.queue.acquire(deadline: .now + .seconds(6)) else { return XCTFail() }
        let controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service(budget: .milliseconds(100))))
        await controller.handlePartial("a ")
        let partial = Task { await controller.handlePartial("a b ") }; await Task.yield()
        let final = controller.finalBarrier("final")
        try? await Task.sleep(for: .milliseconds(4300))
        f.queue.release(held); await partial.value
        let result = await final.value
        XCTAssertEqual(result.reason, "input_deadlineExceeded"); XCTAssertEqual(result.effect, .noEffects)
        XCTAssertFalse(result.permitsFreshFallback); XCTAssertTrue(f.events.isEmpty)

        let previous = InputFixture(capacity: 0); defer { previous.close() }
        let committed = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: previous.service()))
        await committed.handlePartial("hello "); await committed.handlePartial("hello there ")
        guard case .success(let block) = await previous.queue.acquire(deadline: .now + .seconds(1)) else { return XCTFail() }
        let refused = await committed.handleFinal("different")
        XCTAssertEqual(refused.reason, "input_busy"); XCTAssertEqual(refused.effect, .partial)
        XCTAssertEqual(previous.events, ["d9", "u9"])
        previous.queue.release(block)
    }
    func testStreamingAcceptanceSharesGlobalBoundIncludingTailWait() async {
        let f = InputFixture(capacity: 8); defer { f.close() }
        guard case .success(let held) = await f.queue.acquire(deadline: .now + .seconds(3)) else { return XCTFail() }
        let controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service()))
        var requests: [Task<Void, Never>] = []
        for i in 0..<16 { requests.append(Task { await controller.handlePartial("word \(i) ") }); await Task.yield() }
        try? await Task.sleep(for: .milliseconds(30))
        XCTAssertEqual(f.queue.pendingCount, 8, "Tail wait обязан резервировать общую bounded FIFO")
        let final = controller.finalBarrier("final")
        f.queue.invalidatePending(); f.queue.release(held)
        for request in requests { await request.value }
        let outcome = await final.value
        XCTAssertEqual(outcome.reason, "input_busy"); XCTAssertTrue(f.events.isEmpty)
    }
    func testUndoCapturesTargetAndSnapshotAndStillSendsUpAfterCancellation() async {
        for drift in ["target", "copy", "deadline"] {
            let f = InputFixture(); defer { f.close() }
            let service = f.service(budget: .milliseconds(40)), undo = PasteUndoService(pasteService: service)
            guard case .success(let held) = await f.queue.acquire(deadline: .now + .seconds(1)) else { return XCTFail() }
            guard let request = undo.sendCmdZ() else { return XCTFail() }
            if drift == "target" { f.pid = 202 }
            else if drift == "copy" { service.putToClipboardUserInitiated("new user copy") }
            else { try? await Task.sleep(for: .milliseconds(60)) }
            f.queue.release(held)
            let result = await request.value
            XCTAssertFalse(result.ok, drift); XCTAssertEqual(result.effect, .noEffects, drift); XCTAssertTrue(f.events.isEmpty, drift)
        }
        let f = InputFixture(); defer { f.close() }
        let undo = PasteUndoService(pasteService: f.service())
        var request: Task<PasteAttemptResult, Never>?
        f.onDown = { _ in request?.cancel() }
        request = undo.sendCmdZ()
        let result = await request!.value
        XCTAssertEqual(f.events, ["d6", "u6"]); XCTAssertEqual(result.effect, .unknown)
    }
    func testActualUndoCannotInterleaveRevisionOrBypassQuarantine() async {
        let f = InputFixture(); defer { f.close() }; let service = f.service()
        let undo = PasteUndoService(pasteService: service)
        var requested = false
        f.onUp = { key in if key == 51 && !requested { requested = true; undo.sendCmdZ() } }
        let revision = await service.revise(deleteCount: 2, text: "replacement", targetPID: 101)
        let end = ContinuousClock.now + .seconds(1)
        while !f.events.contains("u6") && ContinuousClock.now < end { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(revision.ok)
        XCTAssertEqual(f.events, ["d51", "u51", "d51", "u51", "d9", "u9", "d6", "u6"])
        f.queue.quarantine(); let before = f.events
        undo.sendCmdZ(); try? await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(f.events, before, "Undo обязан уважать quarantine общей очереди")
    }
    func testRealStreamingPartialRevisionDoesNotRepeatDeleteOrFullFallback() async {
        let f = InputFixture(); defer { f.close() }; let service = f.service()
        let controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: service))
        await controller.handlePartial("hello ")
        await controller.handlePartial("hello there ")
        f.failAfterDeletePairs = 2
        let final = await controller.handleFinal("different")
        XCTAssertEqual(final.effect, .partial); XCTAssertEqual(final.backspacePairs, 2)
        let before = f.events
        _ = NSApplication.shared
        let notifications = InputNotificationSpy()
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: notifications), pasteService: service)
        delegate.performAutoPaste(text: "different", historyId: nil, target: nil, streamingOutcome: final)
        XCTAssertTrue(notifications.scripts.contains { $0.contains("частично или с неизвестным результатом") })
        XCTAssertEqual(f.events, before)
        XCTAssertEqual(f.board.string(forType: .string), "hello ")
        let repeated = await controller.handleFinal("different")
        XCTAssertEqual(repeated.effect, .partial); XCTAssertEqual(f.events, before)
    }
    func testLiveOwnedChildRetainsLeaseThroughCallerCancelAndDeadlineCleanup() async {
        let f = InputFixture(); defer { f.close() }
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("ear-owned-child-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: file) }
        let runner = SystemAudioDuckingCommandRunner(executable: "/bin/sh", arguments: { _ in
            ["-c", "echo $$ > \"$1\"; exec /bin/sleep 30", "fixture", file.path]
        })
        let service = f.service(runner: runner, trusted: false, budget: .milliseconds(800))
        let operation = Task { await service.pasteToFrontmostApp("new", targetPID: 101) }
        let deadline = ContinuousClock.now + .milliseconds(650)
        var child: pid_t?
        while ContinuousClock.now < deadline {
            if let text = try? String(contentsOf: file, encoding: .utf8), let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)) { child = pid; break }
            try? await Task.sleep(for: .milliseconds(5))
        }
        if let child { XCTAssertEqual(kill(child, 0), 0, "Собственный child ещё жив") } else { XCTFail("Нет owning child fixture") }
        operation.cancel()
        let pending = await f.queue.acquire(deadline: .now + .milliseconds(30))
        if case .failure(.deadlineExceeded) = pending {} else { XCTFail("Lease освобождена до child cleanup") }
        let result = await operation.value
        XCTAssertEqual(result.effect, .unknown)
        XCTAssertEqual(result.reason, "automation_deadlineExceeded")
        guard case .success(let fresh) = await f.queue.acquire(deadline: .now + .milliseconds(50)) else { return XCTFail("cleanupConfirmed должен освободить lease") }
        f.queue.release(fresh)
        XCTAssertTrue(f.events.isEmpty)
    }

}

private struct NoInputNotifications: NotificationProcessRunning {
    func run(executableURL: URL, arguments: [String]) throws {}
}

private final class InputNotificationSpy: NotificationProcessRunning, @unchecked Sendable {
    var scripts: [String] = []
    func run(executableURL: URL, arguments: [String]) throws { scripts.append(arguments.last ?? "") }
}

@MainActor private final class CapturedQuickEdit: QuickEditPresenting {
    var completions: [(QuickEditResult) -> Void] = []
    func show(text: String, timeoutSec: Double, onAddToVocabulary: ((String) -> Void)?,
              completion: @escaping (QuickEditResult) -> Void) { completions.append(completion) }
}

/// Живая проводка final IPC результата вместо поиска старого имени метода в source.
@MainActor final class StreamingPasteFinalWiringSourceContractTests: XCTestCase {
    func testAuthoritativeFinalResultIsAwaitedAndCannotMutateNewGeneration() async {
        await verifyFinalWiring(enabled: true)
    }
    func testFinalPublicEntryAfterUserCopyDoesNotFallbackWithFreshSnapshot() async {
        let f = InputFixture(); defer { f.close() }; _ = NSApplication.shared
        let service = f.service(), controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service()))
        let notifications = InputNotificationSpy()
        var statuses: [String] = []
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: notifications),
            pasteService: service, pasteTargetActivator: PasteTargetActivator(frontmostPID: { 101 }),
            pasteActivationTarget: { _ in PasteActivationTarget(pid: 101, isAlive: { true }, requestActivation: { false }) },
            pasteFrontmostApplication: { nil }, pasteStatusUpdater: { _, status in statuses.append(status) }, playPasteCompletionSound: {})
        delegate.streamingPasteController = controller
        delegate.settings.streamingPasteEnabled = true; delegate.settings.autoPaste = true
        delegate.settings.quickEditEnabled = false; delegate.settings.clipboardMode = "always_copy"
        delegate.recordingTargetApp = NSRunningApplication.current
        delegate.handleTranscriptionResult(text: "authoritative", historyId: "fixture")
        service.putToClipboardUserInitiated("new user copy")
        let end = ContinuousClock.now + .seconds(2)
        while statuses.isEmpty && ContinuousClock.now < end { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(statuses, ["failed"]); XCTAssertTrue(f.events.isEmpty)
        XCTAssertEqual(f.board.string(forType: .string), "new user copy")
        XCTAssertTrue(notifications.scripts.contains { $0.contains("Потоковая вставка не выполнена. Текст сохранён в истории.") })
        XCTAssertFalse(notifications.scripts.contains { $0.contains("частично или с неизвестным результатом") })
    }
    func testDisabledStreamingLeavesOneOrdinaryPaste() async {
        await verifyFinalWiring(enabled: false)
    }
    private func verifyFinalWiring(enabled: Bool) async {
        let f = InputFixture(); defer { f.close() }; _ = NSApplication.shared
        let service = f.service(), controller = StreamingPasteController(pasteService: PasteServiceStreamingAdapter(pasteService: f.service()))
        var statuses: [String] = []
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: f.defaults, notificationService: NotificationService(processRunner: NoInputNotifications()),
            pasteService: service, pasteTargetActivator: PasteTargetActivator(frontmostPID: { 101 }),
            pasteActivationTarget: { _ in PasteActivationTarget(pid: 101, isAlive: { true }, requestActivation: { false }) },
            pasteFrontmostApplication: { nil }, pasteStatusUpdater: { _, status in statuses.append(status) },
            playPasteCompletionSound: {})
        delegate.streamingPasteController = controller
        delegate.settings.streamingPasteEnabled = enabled; delegate.settings.autoPaste = true
        delegate.settings.quickEditEnabled = false; delegate.settings.clipboardMode = "never_copy"
        if enabled {
            await controller.handlePartial("hello "); await controller.handlePartial("hello there ")
        }
        delegate.recordingTargetApp = NSRunningApplication.current
        delegate.handleTranscriptionResult(text: "authoritative", historyId: "fixture")
        controller.beginSession(); delegate.recordingTargetApp = nil
        let end = ContinuousClock.now + .seconds(2)
        while statuses.isEmpty && ContinuousClock.now < end { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(statuses, ["ok"])
        XCTAssertEqual(f.board.string(forType: .string), "authoritative")
        XCTAssertEqual(f.events.filter { $0 == "d9" }.count, enabled ? 2 : 1,
                       "Final barrier и обычный delegate fallback не должны повторять полный текст")
        XCTAssertFalse(controller.didStreamThisRecording, "Поздний G1 не меняет activity G2")
    }
}
