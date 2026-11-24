import XCTest
@testable import KrabEarAgent

final class WakeWordConsentOutcomeTests: XCTestCase {
    func testTransportFailureIsNotEnabled() {
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: nil), .failed)
    }

    func testBackendRejectionIsNotEnabled() {
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": true, "result": ["ok": false]]), .failed)
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": false]), .failed)
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": true]), .failed)
    }

    func testConfirmedIsEnabled() {
        XCTAssertEqual(WakeWordConsentOutcome.resolve(response: ["ok": true, "result": ["ok": true]]), .enabled)
    }

    func testKrabModelPreferredOnlyWhenNothingChosen() {
        XCTAssertEqual(WakeWordModelChoice.preferredDefault(saved: nil, available: ["hey_jarvis", "krab_ru"]), "krab_ru")
        XCTAssertEqual(WakeWordModelChoice.preferredDefault(saved: "", available: ["krab_ru"]), "krab_ru")
        XCTAssertNil(WakeWordModelChoice.preferredDefault(saved: "alexa", available: ["alexa", "krab_ru"]))
        XCTAssertNil(WakeWordModelChoice.preferredDefault(saved: nil, available: ["hey_jarvis", "alexa"]))
    }
}
