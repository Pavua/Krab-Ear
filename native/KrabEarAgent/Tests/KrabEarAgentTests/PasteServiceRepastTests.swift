/*
 PasteServiceRepastTests.swift
 Тесты для функционала быстрого повтора вставки в PasteService.
*/

import XCTest
@testable import KrabEarAgent

final class PasteServiceRepastTests: XCTestCase {

    var service: PasteService!
    let testKey = "KrabEar_LastPastedText"

    override func setUp() {
        super.setUp()
        service = PasteService()
        // Очищаем UserDefaults перед каждым тестом
        UserDefaults.standard.removeObject(forKey: testKey)
    }

    override func tearDown() {
        UserDefaults.standard.removeObject(forKey: testKey)
        super.tearDown()
    }

    // MARK: - recordLastPaste

    func testRecordLastPasteSavesToUserDefaults() {
        service.recordLastPaste("Hello world")
        let stored = UserDefaults.standard.string(forKey: testKey)
        XCTAssertEqual(stored, "Hello world")
    }

    func testLastPastedTextReturnsNilWhenEmpty() {
        // UserDefaults очищен в setUp
        XCTAssertNil(service.lastPastedText)
    }

    func testLastPastedTextPersistsAcrossInstances() {
        service.recordLastPaste("Persistent text")
        // Создаём новый экземпляр — должен прочитать из UserDefaults
        let service2 = PasteService()
        XCTAssertEqual(service2.lastPastedText, "Persistent text")
    }

    // MARK: - repastLast

    func testRepastLastReturnsNoLastPasteWhenEmpty() {
        let result = service.repastLast()
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "no_last_paste")
    }

    func testRepastLastReturnsRepasteTooSoonWithinCooldown() {
        // Записываем текст и сразу вызываем повтор
        service.recordLastPaste("Quick text")
        // Cooldown 1.0s — вызов сразу после должен вернуть too_soon
        let result = service.repastLast()
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "repaste_too_soon")
    }

    func testRepastLastAllowedAfterCooldown() {
        // Устанавливаем lastPastedAt в прошлом через UserDefaults
        service.lastPastedText = "Old text"
        // Не вызываем recordLastPaste, поэтому lastPastedAt = nil
        // Это симулирует «вставка была в прошлой сессии»
        let result = service.repastLast()
        // Должен попытаться вставить (не вернуть cooldown-ошибку)
        // Реальная вставка не удастся (нет target app), но reason не "repaste_too_soon"
        XCTAssertNotEqual(result.reason, "repaste_too_soon")
        XCTAssertNotEqual(result.reason, "no_last_paste")
    }

    func testCooldownValueIs1Second() {
        XCTAssertEqual(service.repasteCooldownSec, 1.0, accuracy: 0.001)
    }
}
