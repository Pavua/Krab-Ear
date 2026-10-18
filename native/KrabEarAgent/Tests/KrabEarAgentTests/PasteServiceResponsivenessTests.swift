import AppKit
import XCTest
@testable import KrabEarAgent

@MainActor
final class PasteServiceResponsivenessTests: XCTestCase {
    func testHeldModifierYieldsToHeartbeatCreatedInsideOSLeaf() async {
        let fixture = PasteResponsivenessFixture()
        defer { fixture.close() }
        var operationCompleted = false
        var heartbeatDuringOperation = false
        var heartbeat: Task<Void, Never>?
        var reads = 0
        let service = PasteService(
            pasteboard: fixture.pasteboard, defaults: fixture.defaults,
            modifierKeysDown: {
                reads += 1
                if reads == 1 {
                    heartbeat = Task { @MainActor in heartbeatDuringOperation = !operationCompleted }
                }
                return true
            },
            accessibilityTrusted: { false },
            keyEventFactory: { _, _, _ in XCTFail("Held modifier не разрешает input"); return nil },
            inputQueue: InputAutomationQueue(), frontmostPID: { 12345 }, targetIsAlive: { _ in true },
            focusRole: { _ in "AXTextArea" }, focusEditable: { _ in true }, collapseSelection: { _ in }
        )
        // await компилируется и для исходного sync API: RED именно assertion.
        let result = await service.pasteToFrontmostApp("synthetic heartbeat", targetPID: 12345)
        operationCompleted = true
        await heartbeat?.value
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "modifiers_stuck")
        XCTAssertGreaterThan(reads, 1)
        XCTAssertTrue(heartbeatDuringOperation, "MainActor не получил heartbeat, созданный внутри held-state leaf")
    }

    func testDeletionYieldsToHeartbeatCreatedInsideFirstKeyDown() async {
        let fixture = PasteResponsivenessFixture()
        defer { fixture.close() }
        var operationCompleted = false
        var heartbeatDuringOperation = false
        var heartbeat: Task<Void, Never>?
        var downs = 0
        var ups = 0
        let before = fixture.pasteboard.changeCount
        let service = PasteService(
            pasteboard: fixture.pasteboard, defaults: fixture.defaults,
            modifierKeysDown: { false }, accessibilityTrusted: { true },
            keyEventFactory: { pid, key, flags in
                XCTAssertEqual(pid, 12345)
                XCTAssertEqual(key, Keycode.delete.rawValue)
                XCTAssertTrue(flags.isEmpty)
                return PasteKeyEventPair(down: {
                    downs += 1
                    if downs == 1 {
                        heartbeat = Task { @MainActor in heartbeatDuringOperation = !operationCompleted }
                    }
                }, up: { ups += 1 })
            },
            inputQueue: InputAutomationQueue(), frontmostPID: { 12345 }, targetIsAlive: { _ in true },
            focusRole: { _ in "AXTextArea" }, focusEditable: { _ in true }, collapseSelection: { _ in }
        )
        let result = await service.deleteBackward(count: 2, targetPID: 12345)
        operationCompleted = true
        await heartbeat?.value
        XCTAssertTrue(result.ok)
        XCTAssertEqual(downs, 2)
        XCTAssertEqual(ups, 2)
        XCTAssertEqual(fixture.pasteboard.changeCount, before)
        XCTAssertTrue(heartbeatDuringOperation, "Цикл удаления заблокировал MainActor между down/up")
    }
}

private final class PasteResponsivenessFixture {
    let suite: String
    let pasteboard: NSPasteboard
    let defaults: UserDefaults
    init() {
        let suite = "PasteResponsiveness.\(UUID().uuidString)"
        self.suite = suite
        pasteboard = NSPasteboard(name: .init(suite))
        defaults = UserDefaults(suiteName: suite)!
    }
    func close() {
        pasteboard.clearContents()
        pasteboard.releaseGlobally()
        defaults.removePersistentDomain(forName: suite)
    }
}
