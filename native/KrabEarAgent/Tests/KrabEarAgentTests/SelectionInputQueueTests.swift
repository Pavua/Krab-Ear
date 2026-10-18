import AppKit
import XCTest
@testable import KrabEarAgent
@MainActor private final class NoSelectionAX: SelectionAccessibilityAccess {
    func readSelection() -> SelectionAXSelection? { nil }
    var acceptsFocus = true
    func acceptsCurrentFocus(element: SelectionAXElement?) -> Bool { acceptsFocus }
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
