import AppKit
import XCTest
@testable import KrabEarAgent

private final class PasteFailureNotifications: NotificationProcessRunning, @unchecked Sendable {
    var scripts: [String] = []
    func run(executableURL: URL, arguments: [String]) throws {
        scripts.append(arguments.last ?? "")
    }
}

@MainActor
final class PasteFailureNotificationTests: XCTestCase {
    // Ловит дефект: nil text / never_copy ошибочно обещают запись в clipboard.
    // Настоящие handler и PasteService; подменён только процесс уведомления.
    func testNoCopyAttemptDoesNotPromiseClipboard() {
        check(text: nil, mode: "always_copy", concealed: false, expectsCopy: false)
    }

    func testNeverCopyDoesNotPromiseClipboard() {
        check(text: "synthetic result", mode: "never_copy", concealed: false, expectsCopy: false)
    }

    func testConcealedRefusalDoesNotPromiseClipboard() {
        check(text: "synthetic result", mode: "always_copy", concealed: true, expectsCopy: false)
    }

    func testSuccessfulCopyMayPromiseClipboard() {
        check(text: "synthetic result", mode: "always_copy", concealed: false, expectsCopy: true)
    }

    private func check(text: String?, mode: String, concealed: Bool, expectsCopy: Bool) {
        _ = NSApplication.shared
        let suite = "PasteFailureNotification.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        let pasteboard = NSPasteboard(name: .init(suite))
        defer {
            pasteboard.clearContents()
            pasteboard.releaseGlobally()
            defaults.removePersistentDomain(forName: suite)
        }
        let types: [NSPasteboard.PasteboardType] = concealed
            ? [.string, .init("org.nspasteboard.ConcealedType")] : [.string]
        pasteboard.declareTypes(types, owner: nil)
        pasteboard.setString("original fixture", forType: .string)
        let before = pasteboard.changeCount
        let notifications = PasteFailureNotifications()
        let delegate = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]), userDefaults: defaults,
            notificationService: NotificationService(processRunner: notifications),
            pasteService: PasteService(pasteboard: pasteboard, defaults: defaults)
        )
        delegate.settings.mode = "menubar" // historyPanel=nil: окна/IPC не создаются.
        delegate.settings.clipboardMode = mode
        delegate.handlePasteFailure(reason: "no_external_target", text: text)
        XCTAssertEqual(notifications.scripts.count, 1)
        XCTAssertEqual(notifications.scripts.first?.contains("истории и буфере обмена"), expectsCopy)
        XCTAssertEqual(pasteboard.string(forType: .string), expectsCopy ? "synthetic result" : "original fixture")
        if !expectsCopy { XCTAssertEqual(pasteboard.changeCount, before) }
    }
}
