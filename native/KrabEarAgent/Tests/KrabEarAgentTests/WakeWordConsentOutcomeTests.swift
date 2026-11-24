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
}
