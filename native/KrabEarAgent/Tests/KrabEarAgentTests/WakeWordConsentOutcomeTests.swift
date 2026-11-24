import XCTest
@testable import KrabEarAgent

final class WakeWordConsentOutcomeTests: XCTestCase {
    func testTransportFailureIsNotEnabled() {
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: nil), .failed)
    }

    func testMissingOrWrongEngineIsNotEnabled() {
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": true]), .failed)
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": true, "result": [:]]), .failed)
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": true, "result": ["wake_word_engine": "disabled"]]), .failed)
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": true, "result": ["translation_mode": "off"]]), .failed)
    }

    /// Реальная форма ответа set_settings: result — словарь настроек без поля ok.
    func testRealSetSettingsShapeIsEnabled() {
        let response: [String: Any] = ["ok": true, "result": ["wake_word_engine": "openwakeword", "translation_mode": "off"]]
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: response), .enabled)
    }
}
