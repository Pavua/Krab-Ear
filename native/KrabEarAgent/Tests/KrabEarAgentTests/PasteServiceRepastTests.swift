/*
 PasteServiceRepastTests — изолированно проверяет quick replay в PasteService.

 Связь с PasteService: private UserDefaults suite хранит только данные теста, а
 repastePerformer заменяет реальную вставку в frontmost-приложение безопасной имитацией.
*/

import XCTest
@testable import KrabEarAgent

final class PasteServiceRepastTests: XCTestCase {

    private let testKey = "KrabEar_LastPastedText"
    private var service: PasteService!
    private var defaults: UserDefaults!
    private var pasteboard: NSPasteboard!
    private var suiteName: String!
    private var repasteCalls: [String] = []

    override func setUp() {
        super.setUp()
        suiteName = "KrabEarRepastTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        pasteboard = NSPasteboard(name: .init("KrabEarRepastTests.\(UUID().uuidString)"))
        service = PasteService(
            pasteboard: pasteboard,
            defaults: defaults,
            repastePerformer: { [weak self] text in
                self?.repasteCalls.append(text)
                return PasteAttemptResult(ok: true, reason: "fake_repaste")
            }
        )
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        pasteboard.clearContents()
        pasteboard.releaseGlobally()
        service = nil
        defaults = nil
        pasteboard = nil
        suiteName = nil
        repasteCalls = []
        super.tearDown()
    }

    // MARK: - recordLastPaste

    /// E4 п.4: продиктованный текст не пишется на диск (UserDefaults = открытый plist).
    func testRecordLastPasteKeepsTextInMemoryOnly() {
        service.recordLastPaste("Hello world")

        XCTAssertEqual(service.lastPastedText, "Hello world")
        XCTAssertNil(defaults.string(forKey: testKey), "последняя вставка не должна попадать в UserDefaults")
    }

    /// E4 п.4: прежние версии оставили текст в UserDefaults открытым текстом — init стирает его.
    func testInitRemovesLegacyPlaintextKey() {
        defaults.set("legacy dictated secret", forKey: testKey)
        let fresh = PasteService(
            pasteboard: pasteboard,
            defaults: defaults,
            repastePerformer: { _ in PasteAttemptResult(ok: true, reason: "fake_repaste") }
        )

        XCTAssertNil(defaults.string(forKey: testKey))
        XCTAssertNil(fresh.lastPastedText)
    }

    func testLastPastedTextReturnsNilWhenEmpty() {
        XCTAssertNil(service.lastPastedText)
    }

    func testLastPastedTextDoesNotSurviveANewInstance() {
        service.recordLastPaste("Transient text")
        let service2 = PasteService(
            pasteboard: pasteboard,
            defaults: defaults,
            repastePerformer: { _ in PasteAttemptResult(ok: true, reason: "fake_repaste") }
        )

        XCTAssertNil(service2.lastPastedText)
    }

    // MARK: - repastLast

    @MainActor func testRepastLastReturnsNoLastPasteWhenEmptyWithoutCallingPerformer() async {
        let result = await service.repastLast()

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "no_last_paste")
        XCTAssertTrue(repasteCalls.isEmpty)
    }

    @MainActor func testRepastLastReturnsRepasteTooSoonWithoutCallingPerformer() async {
        service.recordLastPaste("Quick text")

        let result = await service.repastLast()

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "repaste_too_soon")
        XCTAssertTrue(repasteCalls.isEmpty)
    }

    @MainActor func testRepastLastAllowedAfterCooldownUsesInjectedPerformer() async {
        // Текст без in-memory timestamp: кулдаун не применяется (первый repaste после установки текста).
        service.lastPastedText = "Old text"

        let result = await service.repastLast()

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.reason, "fake_repaste")
        XCTAssertEqual(repasteCalls, ["Old text"])
    }

    func testCooldownValueIs1Second() {
        XCTAssertEqual(service.repasteCooldownSec, 1.0, accuracy: 0.001)
    }
}
