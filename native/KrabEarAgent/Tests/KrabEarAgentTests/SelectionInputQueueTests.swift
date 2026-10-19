import AppKit
import XCTest
@testable import KrabEarAgent
@MainActor private final class NoSelectionAX: SelectionAccessibilityAccess {
    var reads = 0, focusReads = 0
    var onRead: (() -> Void)?
    func readSelection() -> SelectionAXSelection? { reads += 1; onRead?(); return nil }
    var acceptsFocus = true
    func acceptsCurrentFocus(element: SelectionAXElement?) -> Bool { focusReads += 1; return acceptsFocus }
    func writeSelection(element: SelectionAXElement, text: String) -> Bool { XCTFail("Нет AX write"); return false }
}
@MainActor private struct NoSelectionMonitor: SelectionEventMonitoring {
    func installKeyDownMonitor(handler: @escaping @Sendable (NSEvent) -> Void) -> Any? { nil }
    func removeMonitor(_ monitor: Any) {}
}
private struct NoSelectionNotifications: NotificationProcessRunning {
    func run(executableURL: URL, arguments: [String]) throws {}
}
private final class SelectionNotificationSpy: NotificationProcessRunning, @unchecked Sendable {
    var scripts: [String] = []
    func run(executableURL: URL, arguments: [String]) throws { scripts.append(arguments.joined(separator: " ")) }
}
@MainActor final class SelectionInputQueueTests: XCTestCase {
    func testCopyPairYieldsToMainActorAndRestoreDoesNotOverwriteNewUserCopy() async {
        let name = "SelectionInput.\(UUID().uuidString)"
        let board = NSPasteboard(name: .init(name)), defaults = UserDefaults(suiteName: name)!
        defer { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
        board.clearContents(); board.setString("original", forType: .string)
        let notifications = SelectionNotificationSpy()
        var copyUp = false, heartbeatDuringCopy = false
        var heartbeat: Task<Void, Never>?
        let translator = SelectionTranslator(
            ipcClient: IPCClient(socketPath: "/unused-fixture.sock"),
            notificationService: NotificationService(processRunner: notifications),
            defaults: defaults, eventMonitor: NoSelectionMonitor(), accessibilityAccess: NoSelectionAX(),
            pasteboard: board, inputQueue: InputAutomationQueue(), targetPID: { 101 }, targetIsAlive: { _ in true },
            keyEvents: { _, key, _ in
                PasteKeyEventPair(down: {
                    if key == 8 { heartbeat = Task { @MainActor in heartbeatDuringCopy = !copyUp } }
                }, up: {
                    board.clearContents()
                    if key == 8 { board.setString("selected", forType: .string); copyUp = true }
                    else { board.setString("new user copy", forType: .string) }
                })
            }, translate: { _ in "translated" }
        )
        await translator.performTranslation()
        await heartbeat?.value
        XCTAssertTrue(heartbeatDuringCopy, "Настоящий translator блокирует MainActor между copy down/up")
        try? await Task.sleep(for: .milliseconds(1100))
        XCTAssertEqual(board.string(forType: .string), "new user copy", "Restore не вправе затирать пользовательское копирование")
        XCTAssertFalse(notifications.scripts.contains { $0.contains("Перевод готов") }, "Copy drift после paste up запрещает success HUD")
    }
    func testPostPasteDriftNeverClaimsSuccessAndCancellationKeepsReadGrace() async {
        for drift in ["target", "secure", "epoch", "cancel", "deadline", "none"] {
            let name = "SelectionOutcome.\(UUID().uuidString)"
            let board = NSPasteboard(name: .init(name)), defaults = UserDefaults(suiteName: name)!
            defer { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
            board.clearContents(); board.setString("original", forType: .string)
            let queue = InputAutomationQueue(), ax = NoSelectionAX(), notifications = SelectionNotificationSpy()
            var pid: pid_t = 101, events: [String] = []
            var operation: Task<Void, Never>?, pasteUp: ContinuousClock.Instant?
            let translator = SelectionTranslator(ipcClient: IPCClient(socketPath: "/unused-fixture.sock"),
                notificationService: NotificationService(processRunner: notifications), defaults: defaults,
                eventMonitor: NoSelectionMonitor(), accessibilityAccess: ax, pasteboard: board, inputQueue: queue,
                targetPID: { pid }, targetIsAlive: { _ in true }, keyEvents: { _, key, _ in
                    PasteKeyEventPair(down: { events.append("d\(key)") }, up: {
                        events.append("u\(key)")
                        if key == 8 { board.clearContents(); board.setString("selected", forType: .string); return }
                        pasteUp = .now
                        switch drift {
                        case "target": pid = 202
                        case "secure": ax.acceptsFocus = false
                        case "epoch": queue.invalidatePending()
                        case "cancel": operation?.cancel()
                        case "deadline": Thread.sleep(forTimeInterval: 5.05) // Только bounded fake OS leaf.
                        default: break
                        }
                    })
                }, translate: { _ in "translated" })
            operation = Task { await translator.performTranslation() }
            await operation!.value
            XCTAssertEqual(events, ["d8", "u8", "d9", "u9"], drift)
            XCTAssertEqual(notifications.scripts.contains { $0.contains("Перевод готов") }, drift == "none", drift)
            if drift == "cancel", let pasteUp {
                XCTAssertGreaterThanOrEqual(pasteUp.duration(to: .now), .milliseconds(950), "Cancellation не ускоряет clipboard restore до чтения target")
            }
        }
    }
    func testExternalCopyWhileReadLeaseWaitsRefusesBeforeAXKeysAndIPC() async {
        for writer in ["external", "own", "duringAX"] {
            let name = "SelectionAccepted.\(UUID().uuidString)"
            let board = NSPasteboard(name: .init(name)), defaults = UserDefaults(suiteName: name)!
            defer { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
            board.clearContents(); board.setString("original", forType: .string)
            let queue = InputAutomationQueue(), ax = NoSelectionAX(), notifications = SelectionNotificationSpy()
            guard case .success(let held) = await queue.acquire(deadline: .now + .seconds(3)) else { return XCTFail() }
            var events: [String] = [], translated = false
            let translator = SelectionTranslator(ipcClient: IPCClient(socketPath: "/unused-fixture.sock"),
                notificationService: NotificationService(processRunner: notifications), defaults: defaults,
                eventMonitor: NoSelectionMonitor(), accessibilityAccess: ax, pasteboard: board, inputQueue: queue,
                targetPID: { 101 }, targetIsAlive: { _ in true }, keyEvents: { _, key, _ in
                    PasteKeyEventPair(down: { events.append("d\(key)") }, up: {
                        events.append("u\(key)")
                        if key == 8 { board.clearContents(); board.setString("selected", forType: .string) }
                    })
                }, translate: { text in XCTAssertEqual(text, "selected"); translated = true; return "translated" })
            let operation = Task { await translator.performTranslation() }
            let end = ContinuousClock.now + .seconds(1)
            while queue.pendingCount == 0 && ContinuousClock.now < end { await Task.yield() }
            guard queue.pendingCount == 1 else { queue.release(held); await operation.value; return XCTFail("Нет accepted translator request") }
            let epoch = queue.generation, before = board.changeCount
            if writer == "duringAX" {
                ax.onRead = { board.clearContents(); board.setString("new duringAX copy", forType: .string) }
            } else { board.clearContents(); board.setString("new \(writer) copy", forType: .string) }
            if writer == "own" { queue.recordClipboardWrite(board: board.name.rawValue, before: before, after: board.changeCount) }
            XCTAssertEqual(queue.generation, epoch, "External clipboard не требует нашего invalidate")
            queue.release(held); await operation.value
            if writer != "own" {
                XCTAssertEqual(ax.reads, writer == "duringAX" ? 1 : 0)
                if writer == "external" { XCTAssertEqual(ax.focusReads, 0) }
                XCTAssertTrue(events.isEmpty); XCTAssertFalse(translated)
                XCTAssertFalse(notifications.scripts.contains { $0.contains("Перевод готов") })
            } else {
                XCTAssertEqual(ax.reads, 1); XCTAssertTrue(translated)
                XCTAssertEqual(events, ["d8", "u8", "d9", "u9"])
                XCTAssertTrue(notifications.scripts.contains { $0.contains("Перевод готов") })
            }
            XCTAssertEqual(board.string(forType: .string), "new \(writer) copy", "Saved clipboard читается после validated own chain")
        }
    }
    func testConcealedCopyAfterKeyUpIsNeverSentToTranslation() async {
        let name = "SelectionConcealed.\(UUID().uuidString)"
        let board = NSPasteboard(name: .init(name)), defaults = UserDefaults(suiteName: name)!
        defer { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
        var translated = false
        let translator = SelectionTranslator(
            ipcClient: IPCClient(socketPath: "/unused-fixture.sock"),
            notificationService: NotificationService(processRunner: NoSelectionNotifications()),
            defaults: defaults, eventMonitor: NoSelectionMonitor(), accessibilityAccess: NoSelectionAX(),
            pasteboard: board, inputQueue: InputAutomationQueue(), targetPID: { 101 }, targetIsAlive: { _ in true },
            keyEvents: { _, _, _ in PasteKeyEventPair(down: {}, up: {
                board.declareTypes([.string, .init("org.nspasteboard.ConcealedType")], owner: nil)
                board.setString("private selection", forType: .string)
            }) }, translate: { _ in translated = true; return nil }
        )
        await translator.performTranslation()
        XCTAssertFalse(translated, "Concealed drift после copy await запрещает передачу текста в IPC")
    }

    /// Fallback через Cmd+C затирает пользовательский clipboard выделенным текстом:
    /// любой выход после копирования (смена цели, drift, отмена) обязан вернуть saved.
    func testClipboardFallbackRestoresSavedOnEveryExitAfterCopy() async {
        for exit in ["targetChangedDuringIPC", "cancelledBeforeIPC"] {
            let name = "SelectionRestore.\(UUID().uuidString)"
            let board = NSPasteboard(name: .init(name)), defaults = UserDefaults(suiteName: name)!
            defer { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
            board.clearContents(); board.setString("важный буфер", forType: .string)
            var pid: pid_t = 101
            var operation: Task<Void, Never>?
            let translator = SelectionTranslator(
                ipcClient: IPCClient(socketPath: "/unused-fixture.sock"),
                notificationService: NotificationService(processRunner: NoSelectionNotifications()),
                defaults: defaults, eventMonitor: NoSelectionMonitor(), accessibilityAccess: NoSelectionAX(),
                pasteboard: board, inputQueue: InputAutomationQueue(), targetPID: { pid }, targetIsAlive: { _ in true },
                keyEvents: { _, key, _ in
                    PasteKeyEventPair(down: {}, up: {
                        if key == 8 {
                            board.clearContents(); board.setString("выделенное", forType: .string)
                            if exit == "cancelledBeforeIPC" { operation?.cancel() }
                        }
                    })
                },
                translate: { _ in
                    if exit == "targetChangedDuringIPC" { pid = 202 }
                    return "перевод"
                })
            operation = Task { await translator.performTranslation() }
            await operation!.value
            XCTAssertEqual(board.string(forType: .string), "важный буфер", exit)
        }
    }

    func testTranslatorOwnsCopyPairButReleasesQueueDuringTranslation() async {
        let name = "SelectionLease.\(UUID().uuidString)"
        let board = NSPasteboard(name: .init(name)), defaults = UserDefaults(suiteName: name)!
        defer { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
        let queue = InputAutomationQueue()
        var copyUp = false, pasteBeforeUp = false, translationHadLease = false
        var competing: Task<PasteAttemptResult, Never>?
        let service = PasteService(pasteboard: board, defaults: defaults, modifierKeysDown: { false },
            accessibilityTrusted: { true }, keyEventFactory: { _, _, _ in
                PasteKeyEventPair(down: { if !copyUp { pasteBeforeUp = true } }, up: {})
            }, inputQueue: queue, frontmostPID: { 101 }, targetIsAlive: { _ in true },
            focusRole: { _ in "AXTextArea" }, focusEditable: { _ in true }, collapseSelection: { _ in })
        let translator = SelectionTranslator(
            ipcClient: IPCClient(socketPath: "/unused-fixture.sock"),
            notificationService: NotificationService(processRunner: NoSelectionNotifications()),
            defaults: defaults, eventMonitor: NoSelectionMonitor(), accessibilityAccess: NoSelectionAX(),
            pasteboard: board, inputQueue: queue, targetPID: { 101 }, targetIsAlive: { _ in true },
            keyEvents: { _, _, _ in PasteKeyEventPair(down: {
                competing = Task { await service.pasteToFrontmostApp("competing", targetPID: 101) }
            }, up: { copyUp = true; board.clearContents(); board.setString("selected", forType: .string) }) },
            translate: { _ in
                _ = await competing?.value
                if case .success(let lease) = await queue.acquire(deadline: .now + .milliseconds(100)) {
                    translationHadLease = true; queue.release(lease)
                }
                return nil
            })
        await translator.performTranslation()
        XCTAssertFalse(pasteBeforeUp); XCTAssertTrue(translationHadLease)
        XCTAssertEqual(board.string(forType: .string), nil)
    }

}
