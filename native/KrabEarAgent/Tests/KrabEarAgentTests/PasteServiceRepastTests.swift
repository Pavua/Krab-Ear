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
        service = nil
        defaults = nil
        pasteboard = nil
        suiteName = nil
        repasteCalls = []
        super.tearDown()
    }

    // MARK: - recordLastPaste

    func testRecordLastPasteSavesToInjectedDefaults() {
        service.recordLastPaste("Hello world")

        XCTAssertEqual(defaults.string(forKey: testKey), "Hello world")
    }

    func testLastPastedTextReturnsNilWhenEmpty() {
        XCTAssertNil(service.lastPastedText)
    }

    func testLastPastedTextPersistsAcrossInstancesWithSameDefaults() {
        service.recordLastPaste("Persistent text")
        let service2 = PasteService(
            pasteboard: pasteboard,
            defaults: defaults,
            repastePerformer: { _ in PasteAttemptResult(ok: true, reason: "fake_repaste") }
        )

        XCTAssertEqual(service2.lastPastedText, "Persistent text")
    }

    // MARK: - repastLast

    func testRepastLastReturnsNoLastPasteWhenEmptyWithoutCallingPerformer() {
        let result = service.repastLast()

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "no_last_paste")
        XCTAssertTrue(repasteCalls.isEmpty)
    }

    func testRepastLastReturnsRepasteTooSoonWithoutCallingPerformer() {
        service.recordLastPaste("Quick text")

        let result = service.repastLast()

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "repaste_too_soon")
        XCTAssertTrue(repasteCalls.isEmpty)
    }

    func testRepastLastAllowedAfterCooldownUsesInjectedPerformer() {
        // Отсутствие in-memory timestamp имитирует восстановление текста из прошлой сессии.
        service.lastPastedText = "Old text"

        let result = service.repastLast()

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.reason, "fake_repaste")
        XCTAssertEqual(repasteCalls, ["Old text"])
    }

    func testCooldownValueIs1Second() {
        XCTAssertEqual(service.repasteCooldownSec, 1.0, accuracy: 0.001)
    }
}
