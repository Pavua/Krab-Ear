import AppKit
import XCTest
@testable import KrabEarAgent

/// Живой баг 27.11: диктовка стартовала в Chrome, стоп — из другого приложения,
/// `activate` вернул false → `target_activation_failed`. Текст ушёл только в историю:
/// ни вставки, ни буфера обмена, хотя `clipboardMode` = `always_copy`.
/// Здесь: настоящие delegate и PasteService; приватные pasteboard/defaults/очередь;
/// клавиатура подменена XCTFail — синтетический ввод в любое приложение запрещён.
private final class FallbackNotifications: NotificationProcessRunning, @unchecked Sendable {
    var scripts: [String] = []
    func run(executableURL: URL, arguments: [String]) throws {
        scripts.append(arguments.joined(separator: "\n"))
    }
}

@MainActor private final class FallbackFixture {
    let board: NSPasteboard, defaults: UserDefaults, queue = InputAutomationQueue(), name: String
    let notifications = FallbackNotifications()
    var activations = 0
    lazy var service = PasteService(
        pasteboard: board, defaults: defaults,
        modifierKeysDown: { false }, accessibilityTrusted: { true },
        keyEventFactory: { _, _, _ in XCTFail("Отказ активации не разрешает синтетический ввод"); return nil },
        inputQueue: queue, frontmostPID: { 202 }, targetIsAlive: { _ in true }
    )

    init(concealed: Bool = false) {
        name = "PasteActivationFallback.\(UUID().uuidString)"
        board = NSPasteboard(name: .init(name)); defaults = UserDefaults(suiteName: name)!
        let types: [NSPasteboard.PasteboardType] = concealed
            ? [.string, .init("org.nspasteboard.ConcealedType")] : [.string]
        board.declareTypes(types, owner: nil)
        board.setString("original fixture", forType: .string)
    }

    /// `requestActivation` = false воспроизводит отказ AppKit из живого лога.
    func delegate(mode: String, activationGranted: Bool = false, completed: (() -> Void)? = nil) -> AgentAppDelegate {
        _ = NSApplication.shared
        let delegate = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]), userDefaults: defaults,
            notificationService: NotificationService(processRunner: notifications),
            pasteService: service,
            pasteTargetActivator: PasteTargetActivator(budget: .milliseconds(60), frontmostPID: { 202 }),
            pasteActivationTarget: { _ in
                PasteActivationTarget(pid: 101, isAlive: { true }, requestActivation: { [unowned self] in
                    self.activations += 1; return activationGranted
                })
            },
            pasteFrontmostApplication: { nil }, playPasteCompletionSound: { completed?() }
        )
        delegate.settings.mode = "menubar" // historyPanel=nil: окна и IPC не создаются.
        delegate.settings.clipboardMode = mode
        delegate.settings.autoPaste = true
        return delegate
    }

    func waitForNotification() async {
        let end = ContinuousClock.now + .seconds(2)
        while notifications.scripts.isEmpty && ContinuousClock.now < end {
            try? await Task.sleep(for: .milliseconds(10))
        }
    }

    func close() { board.clearContents(); board.releaseGlobally(); defaults.removePersistentDomain(forName: name) }
}

@MainActor
final class PasteActivationClipboardFallbackTests: XCTestCase {
    // MARK: - Публичные входы (тот самый путь из лога)

    func testDictationActivationRefusalCopiesTextAndSaysSo() async {
        for mode in ["always_copy", "copy_on_fail"] {
            let f = FallbackFixture(); defer { f.close() }
            let delegate = f.delegate(mode: mode)
            delegate.performAutoPaste(text: "диктовка из Chrome", historyId: nil, target: NSRunningApplication.current)
            await f.waitForNotification()
            withExtendedLifetime(delegate) {}
            XCTAssertEqual(f.activations, 1, mode)
            XCTAssertEqual(f.board.string(forType: .string), "диктовка из Chrome", mode)
            XCTAssertEqual(f.notifications.scripts.count, 1, mode)
            let body = f.notifications.scripts.first ?? ""
            XCTAssertTrue(body.contains("скопирован в буфер обмена"), "\(mode): \(body)")
            XCTAssertTrue(body.contains("вставьте вручную"), "\(mode): \(body)")
        }
    }

    func testSnapshotPasteActivationRefusalAlsoCopies() async {
        let f = FallbackFixture(); defer { f.close() }
        let delegate = f.delegate(mode: "always_copy")
        delegate.recordingTargetApp = NSRunningApplication.current
        delegate.pasteSnapshotText(text: "из истории", historyId: nil, sourceTag: "fixture")
        await f.waitForNotification()
        withExtendedLifetime(delegate) {}
        XCTAssertEqual(f.activations, 1)
        XCTAssertEqual(f.board.string(forType: .string), "из истории")
        XCTAssertTrue(f.notifications.scripts.first?.contains("скопирован в буфер обмена") ?? false)
    }

    func testNeverCopyKeepsClipboardAndOldMessage() async {
        let f = FallbackFixture(); defer { f.close() }
        let before = f.board.changeCount
        // Task вставки держит delegate слабо: без сильной ссылки он молча выходит.
        let delegate = f.delegate(mode: "never_copy")
        delegate.performAutoPaste(text: "секрет", historyId: nil, target: NSRunningApplication.current)
        await f.waitForNotification()
        withExtendedLifetime(delegate) {}
        XCTAssertEqual(f.activations, 1)
        XCTAssertEqual(f.notifications.scripts.count, 1)
        XCTAssertEqual(f.board.string(forType: .string), "original fixture")
        XCTAssertEqual(f.board.changeCount, before)
        let body = f.notifications.scripts.first ?? ""
        XCTAssertFalse(body.contains("буфер"), body)
        XCTAssertTrue(body.contains("истории"), body)
    }

    func testConcealedClipboardIsNotOverwrittenAndMessageIsHonest() async {
        let f = FallbackFixture(concealed: true); defer { f.close() }
        let before = f.board.changeCount
        // Task вставки держит delegate слабо: без сильной ссылки он молча выходит.
        let delegate = f.delegate(mode: "always_copy")
        delegate.performAutoPaste(text: "диктовка", historyId: nil, target: NSRunningApplication.current)
        await f.waitForNotification()
        withExtendedLifetime(delegate) {}
        XCTAssertEqual(f.activations, 1)
        XCTAssertEqual(f.notifications.scripts.count, 1)
        XCTAssertEqual(f.board.string(forType: .string), "original fixture")
        XCTAssertEqual(f.board.changeCount, before)
        let body = f.notifications.scripts.first ?? ""
        XCTAssertFalse(body.contains("скопирован в буфер обмена"), body)
        XCTAssertTrue(body.contains("защищён"), body)
    }

    // MARK: - Когда копия запрещена: правило «поздняя запись stale text»

    func testBusyAndCancelledRefusalsNeverCopy() {
        for reason in ["target_activation_busy", "target_activation_cancelled"] {
            let f = FallbackFixture(); defer { f.close() }
            let delegate = f.delegate(mode: "always_copy")
            let fallback = PasteClipboardFallback(text: "stale", snapshot: f.service.captureInput(pid: 101))
            let before = f.board.changeCount
            delegate.handlePasteFailure(reason: reason, fallback: fallback)
            XCTAssertEqual(f.board.string(forType: .string), "original fixture", reason)
            XCTAssertEqual(f.board.changeCount, before, reason)
            XCTAssertFalse(f.notifications.scripts.first?.contains("буфер") ?? true, reason)
        }
    }

    func testTargetSideRefusalsCopyWhenNothingChanged() {
        for reason in ["target_activation_failed", "target_activation_timeout", "target_activation_terminated"] {
            let f = FallbackFixture(); defer { f.close() }
            let delegate = f.delegate(mode: "always_copy")
            let fallback = PasteClipboardFallback(text: "свежий текст", snapshot: f.service.captureInput(pid: 101))
            delegate.handlePasteFailure(reason: reason, fallback: fallback)
            XCTAssertEqual(f.board.string(forType: .string), "свежий текст", reason)
        }
    }

    func testNewerUserCopyAfterSnapshotIsNotOverwritten() {
        let f = FallbackFixture(); defer { f.close() }
        let delegate = f.delegate(mode: "always_copy")
        let fallback = PasteClipboardFallback(text: "stale", snapshot: f.service.captureInput(pid: 101))
        f.board.clearContents(); f.board.setString("new user copy", forType: .string)
        delegate.handlePasteFailure(reason: "target_activation_failed", fallback: fallback)
        XCTAssertEqual(f.board.string(forType: .string), "new user copy")
        XCTAssertFalse(f.notifications.scripts.first?.contains("скопирован в буфер обмена") ?? true)
    }

    func testOwnClipboardWriteAfterSnapshotBlocksCopy() {
        let f = FallbackFixture(); defer { f.close() }
        let delegate = f.delegate(mode: "always_copy")
        let fallback = PasteClipboardFallback(text: "stale", snapshot: f.service.captureInput(pid: 101))
        f.service.putToClipboardUserInitiated("copy from history panel")
        delegate.handlePasteFailure(reason: "target_activation_failed", fallback: fallback)
        XCTAssertEqual(f.board.string(forType: .string), "copy from history panel")
    }

    /// Ждущая в очереди более новая операция: наша поздняя запись инвалидировала бы её.
    func testPendingNewerOperationIsNotInvalidated() {
        let f = FallbackFixture(); defer { f.close() }
        let delegate = f.delegate(mode: "always_copy")
        let fallback = PasteClipboardFallback(text: "stale", snapshot: f.service.captureInput(pid: 101))
        let newer = f.service.reserveInput(target: 101)
        defer { newer.cancel() }
        XCTAssertNil(newer.failure)
        let epoch = f.queue.generation
        delegate.handlePasteFailure(reason: "target_activation_failed", fallback: fallback)
        XCTAssertEqual(f.queue.generation, epoch, "Новая операция в очереди должна остаться валидной")
        XCTAssertEqual(f.board.string(forType: .string), "original fixture")
    }

    func testNoFallbackKeepsPreviousBehaviour() {
        let f = FallbackFixture(); defer { f.close() }
        let delegate = f.delegate(mode: "always_copy")
        delegate.handlePasteFailure(reason: "target_activation_failed")
        XCTAssertEqual(f.board.string(forType: .string), "original fixture")
        XCTAssertTrue(f.notifications.scripts.first?.contains("Текст сохранён в истории.") ?? false)
    }

    // MARK: - Проводка call sites

    func testBothActivationCallSitesPassFallbackFromTheirOwnReservation() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: root.appendingPathComponent("Sources/KrabEarAgent/main+PasteHandling.swift"), encoding: .utf8)
        for (start, end) in [("    func performAutoPaste(", "    func isDuplicateAutopasteCandidate("),
                             ("    func pasteSnapshotText(", "    func handlePasteFailure(")] {
            let lower = try XCTUnwrap(source.range(of: start)).lowerBound
            let upper = try XCTUnwrap(source.range(of: end)).lowerBound
            let body = String(source[lower..<upper])
            XCTAssertTrue(
                body.contains("handlePasteFailure(reason: pasteResult.reason, fallback: request.snapshot.map { PasteClipboardFallback(text: textToInsert, snapshot: $0) })"),
                start
            )
        }
    }
}
