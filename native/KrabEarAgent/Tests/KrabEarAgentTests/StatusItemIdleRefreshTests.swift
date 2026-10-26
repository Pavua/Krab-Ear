import AppKit
import XCTest
@testable import KrabEarAgent

private final class StatusImageWrites: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    func increment() { lock.withLock { value += 1 } }
    func reset() { lock.withLock { value = 0 } }
    var count: Int { lock.withLock { value } }
}

/// Настоящий callback агента и AppKit button; ни startup, ни IPC, ни модель.
@MainActor
final class StatusItemIdleRefreshWiringTests: XCTestCase {
    private func withDelegate(_ body: (AgentAppDelegate, NSStatusBarButton) -> Void) {
        _ = NSApplication.shared
        let defaults = IsolatedUserDefaultsDomain(scope: "StatusItemIdleRefreshTests")
        let delegate = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: defaults.defaults
        )
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.isVisible = false
        delegate.statusItem = item
        defer {
            delegate.statusItem = nil
            NSStatusBar.system.removeStatusItem(item)
            defaults.removePersistentDomain()
        }
        guard let button = item.button else {
            XCTFail("AppKit не создал status button")
            return
        }
        body(delegate, button)
    }

    func test_unchanged_background_ticks_do_not_write_image_again() {
        withDelegate { delegate, button in
            let writes = StatusImageWrites()
            let observation = button.observe(\.image, options: [.new]) { _, _ in
                writes.increment()
            }
            defer { observation.invalidate() }
            delegate.applyHealthStateToStatusItem(.healthy)
            XCTAssertGreaterThan(writes.count, 0, "KVO должен наблюдать первый настоящий render")
            writes.reset()
            for _ in 0..<10 { delegate.applyHealthStateToStatusItem(.healthy) }
            XCTAssertEqual(writes.count, 0, "Идентичный tick заново трогает AppKit image")
        }
    }

    func test_recording_transition_renders_once_then_stays_quiet() {
        withDelegate { delegate, button in
            delegate.applyHealthStateToStatusItem(.healthy)
            let writes = StatusImageWrites()
            let observation = button.observe(\.image, options: [.new]) { _, _ in writes.increment() }
            defer { observation.invalidate() }
            delegate.isRecording = true
            delegate.applyHealthStateToStatusItem(.healthy)
            XCTAssertNotNil(button.image)
            XCTAssertGreaterThan(writes.count, 0, "Начало записи обязано обновить indicator")
            writes.reset()
            for _ in 0..<10 { delegate.applyHealthStateToStatusItem(.healthy) }
            XCTAssertEqual(writes.count, 0, "Recording image пересоздаётся при неизменном состоянии")
            delegate.isRecording = false
            delegate.isProcessing = true
            delegate.applyHealthStateToStatusItem(.healthy)
            XCTAssertTrue(button.title.contains("…"))
        }
    }

    /// Реальный stop диктовки держит isRecording=true до терминального ответа
    /// (токен нужен recovery) и одновременно ставит isProcessing=true.
    func test_dictation_finalization_shows_processing_not_recording() {
        withDelegate { delegate, button in
            delegate.isRecording = true
            delegate.isProcessing = true
            delegate.refreshStatusItemTitle()
            XCTAssertTrue(button.title.contains("…"), "Финализация должна читаться как обработка")
            XCTAssertNil(button.image, "Красная точка записи не должна гореть во время финализации")

            delegate.isProcessing = false
            delegate.refreshStatusItemTitle()
            XCTAssertFalse(button.title.contains("…"))
            XCTAssertNotNil(button.image, "Recovery без активного IPC остаётся записью с красной точкой")
        }
    }

    func test_external_temporary_title_is_restored_on_next_tick() {
        withDelegate { delegate, button in
            delegate.applyHealthStateToStatusItem(.healthy)
            let expected = button.title
            button.title = "synthetic temporary title"
            delegate.applyHealthStateToStatusItem(.healthy)
            XCTAssertEqual(button.title, expected)
        }
    }

    func test_health_privacy_severity_and_appearance_changes_are_rendered() {
        withDelegate { delegate, button in
            button.appearance = NSAppearance(named: .aqua)
            delegate.applyHealthStateToStatusItem(.healthy)
            let writes = StatusImageWrites()
            let observation = button.observe(\.image, options: [.new]) { _, _ in writes.increment() }
            defer { observation.invalidate() }
            let changes: [() -> Void] = [
                { delegate.applyHealthStateToStatusItem(.hung) },
                {
                    delegate.privacyModeEnabled = true
                    delegate.applyHealthStateToStatusItem(.hung)
                },
                {
                    delegate.statusErrorBadgeSeverity = "warn"
                    delegate.applyHealthStateToStatusItem(.hung)
                },
                {
                    button.appearance = NSAppearance(named: .darkAqua)
                    delegate.applyHealthStateToStatusItem(.hung)
                },
            ]
            for change in changes {
                writes.reset()
                change()
                XCTAssertGreaterThan(writes.count, 0, "Изменившийся вход обязан вызвать render")
                writes.reset()
                delegate.applyHealthStateToStatusItem(.hung)
                XCTAssertEqual(writes.count, 0)
            }
        }
    }

    func test_external_image_and_position_changes_are_reconciled() {
        withDelegate { delegate, button in
            delegate.applyHealthStateToStatusItem(.healthy)
            XCTAssertNil(button.image)
            button.image = NSImage(size: NSSize(width: 4, height: 4))
            button.imagePosition = .imageAbove
            delegate.applyHealthStateToStatusItem(.healthy)
            XCTAssertNil(button.image)
            XCTAssertEqual(button.imagePosition, .imageLeft)
        }
    }

    func test_recreated_status_button_is_not_mistaken_for_cached_button() {
        withDelegate { delegate, _ in
            delegate.applyHealthStateToStatusItem(.healthy)
            let replacement = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            replacement.isVisible = false
            defer { NSStatusBar.system.removeStatusItem(replacement) }
            delegate.statusItem = replacement
            delegate.applyHealthStateToStatusItem(.healthy)
            XCTAssertEqual(replacement.button?.title, "KE [D]")
        }
    }

}
