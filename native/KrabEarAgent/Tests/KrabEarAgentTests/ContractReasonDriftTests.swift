import XCTest
@testable import KrabEarAgent

/// Сверка строк-причин Swift с тем, что реально отдаёт Python backend
/// (`plaintext_export_authorization.py`, `service.py::_timeline_export_denied`).
final class ContractReasonDriftTests: XCTestCase {

    // MARK: Timeline export

    func test_timeline_authorizer_denial_privacy_mode_active_is_privacy() {
        // service.py::_timeline_export_denied: error.code == reason
        let result: [String: Any] = [
            "ok": false, "reason": "privacy_mode_active", "path": NSNull(),
            "error": ["code": "privacy_mode_active", "message": "privacy_mode_active"],
        ]
        XCTAssertEqual(TimelineExportOutcome.classify(result), .privacy)
    }

    func test_timeline_legacy_settings_gate_is_privacy() {
        // service.py: {"reason": "privacy_mode_active", error.code: "privacy_mode"}
        let result: [String: Any] = [
            "ok": false, "reason": "privacy_mode_active",
            "error": ["code": "privacy_mode", "message": "x"],
        ]
        XCTAssertEqual(TimelineExportOutcome.classify(result), .privacy)
    }

    func test_timeline_old_code_only_is_still_privacy() {
        let result: [String: Any] = ["error": ["code": "privacy_mode", "message": "x"]]
        XCTAssertEqual(TimelineExportOutcome.classify(result), .privacy)
    }

    func test_timeline_invalid_path_is_failure_with_message() {
        let result: [String: Any] = ["error": ["code": "invalid_path", "message": "bad dir"]]
        XCTAssertEqual(TimelineExportOutcome.classify(result), .failure("bad dir"))
    }

    func test_timeline_success_returns_path() {
        let result: [String: Any] = ["path": "/tmp/x.svg", "blocks": 3]
        XCTAssertEqual(TimelineExportOutcome.classify(result), .saved("/tmp/x.svg"))
    }

    func test_timeline_empty_path_is_empty_path_outcome() {
        XCTAssertEqual(TimelineExportOutcome.classify(["path": ""]), .emptyPath)
        XCTAssertEqual(TimelineExportOutcome.classify([:]), .emptyPath)
    }

    // MARK: PlaintextExportCoordinator reason mapping

    @MainActor
    func test_every_python_plaintext_reason_maps_to_its_own_failure() {
        // Все четыре значения PlaintextExportDenied.allowed (Python).
        typealias C = PlaintextExportCoordinator
        XCTAssertEqual(C.failure(forReason: "privacy_mode_active"), .privacy)
        XCTAssertEqual(C.failure(forReason: "plaintext_policy_unavailable"), .unavailable)
        XCTAssertEqual(C.failure(forReason: "plaintext_session_expired"), .expired)
        XCTAssertEqual(C.failure(forReason: "plaintext_confirmation_required"), .expired)
    }

    @MainActor
    func test_unknown_reason_stays_fail_closed_unavailable() {
        typealias C = PlaintextExportCoordinator
        XCTAssertEqual(C.failure(forReason: "something_new"), .unavailable)
        XCTAssertEqual(C.failure(forReason: nil), .unavailable)
        XCTAssertEqual(C.failure(forReason: 42), .unavailable)
    }
}
