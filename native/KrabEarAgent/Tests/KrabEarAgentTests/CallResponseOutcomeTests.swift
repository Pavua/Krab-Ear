/*
 CallResponseOutcomeTests — E4 п.2: ответ backend на call_start / call_session_end читается строго.

 Раньше:
 - call_start с `result.ok == false` (не «не настроен») или без `session_id` создавал фантомную
   сессию со случайным UUID и запускал polling несуществующего звонка;
 - отказ call_session_end (`try?` проглатывал ошибку) всё равно помечал сессию `.ended`,
   хотя звонок мог оставаться живым.
*/

import XCTest
@testable import KrabEarAgent

final class CallResponseOutcomeTests: XCTestCase {

    // MARK: call_start

    func test_dial_nilResponse_isMissing() {
        XCTAssertEqual(CallDialOutcome.parse(nil), .missingResponse)
    }

    func test_dial_success_requiresSessionID() {
        XCTAssertEqual(
            CallDialOutcome.parse(["result": ["ok": true, "session_id": "s-1", "status": "dialing"]]),
            .started(sessionID: "s-1", status: "dialing")
        )
        guard case .failed = CallDialOutcome.parse(["result": ["ok": true]]) else {
            return XCTFail("успех без session_id обязан быть отказом, а не фантомной сессией")
        }
        guard case .failed = CallDialOutcome.parse(["result": ["ok": true, "session_id": "  "]]) else {
            return XCTFail("пустой session_id обязан быть отказом")
        }
        guard case .failed = CallDialOutcome.parse(["result": ["session_id": 42]]) else {
            return XCTFail("session_id не строка обязан быть отказом")
        }
    }

    func test_dial_missingStatusDefaultsToDialing() {
        XCTAssertEqual(
            CallDialOutcome.parse(["result": ["session_id": "s-2"]]),
            .started(sessionID: "s-2", status: "dialing")
        )
    }

    func test_dial_payloadRefusal_isFailureWithCode() {
        let outcome = CallDialOutcome.parse(["result": ["ok": false, "error": "gateway_timeout"]])
        guard case .failed(let message) = outcome else { return XCTFail("\(outcome)") }
        XCTAssertTrue(message.contains("gateway_timeout"), message)
    }

    func test_dial_notConfigured_inPayloadAndEnvelope() {
        for code in ["gateway_not_configured", "call_provider_unavailable"] {
            XCTAssertEqual(
                CallDialOutcome.parse(["result": ["ok": false, "error": code]]), .needsConfiguration, code
            )
            XCTAssertEqual(
                CallDialOutcome.parse(["error": ["code": code, "message": "x"]]), .needsConfiguration, code
            )
        }
    }

    func test_dial_otherEnvelopeError_isFailure() {
        guard case .failed = CallDialOutcome.parse(["error": ["code": "internal_error", "message": "boom"]]) else {
            return XCTFail("ошибка конверта обязана быть отказом")
        }
    }

    func test_dial_noResultNoError_isFailure() {
        guard case .failed = CallDialOutcome.parse([:]) else { return XCTFail() }
    }

    // MARK: call_session_end

    func test_end_nilOrErrorEnvelope_isFailure() {
        guard case .failed = CallEndOutcome.parse(nil) else { return XCTFail("nil = отказ, звонок мог остаться живым") }
        guard case .failed = CallEndOutcome.parse(["error": ["code": "internal_error", "message": "x"]]) else {
            return XCTFail("ошибка конверта = отказ")
        }
        guard case .failed = CallEndOutcome.parse([:]) else { return XCTFail("нет result = отказ") }
    }

    func test_end_payloadRefusal_isFailure() {
        guard case .failed = CallEndOutcome.parse(["result": ["ok": false, "error": "gateway_unreachable"]]) else {
            return XCTFail("ok=false в payload = отказ")
        }
    }

    func test_end_success_variants() {
        // Backend отвечает {session_id, status, ...} без ключа ok; явный ok=true тоже успех.
        XCTAssertEqual(CallEndOutcome.parse(["result": ["session_id": "s", "status": "completed"]]), .ended)
        XCTAssertEqual(CallEndOutcome.parse(["result": ["ok": true]]), .ended)
    }

    /// Ревью ChatGPT (2026-11-16): пустой/неоднозначный ответ не подтверждает завершение.
    func test_end_malformedOrContradictoryAck_isFailure() {
        for result in [[String: Any](),
                       ["ok": "false"],
                       ["ok": "true"],
                       ["ok": true, "status": "active"],
                       ["session_id": "s", "status": "talking"],
                       ["status": "ending"]] as [[String: Any]] {
            guard case .failed = CallEndOutcome.parse(["result": result]) else {
                return XCTFail("\(result) ошибочно принят как подтверждённое завершение")
            }
        }
    }

    func test_end_numericOkOne_isNotAcknowledgement() {
        guard case .failed = CallEndOutcome.parse(["result": ["ok": NSNumber(value: 1)]]) else {
            return XCTFail("числовой ok:1 не JSON-boolean и не подтверждает завершение")
        }
    }

    func test_end_terminalStatusWithoutOk_isEnded() {
        XCTAssertEqual(CallEndOutcome.parse(["result": ["session_id": "s", "status": "failed"]]), .ended)
    }

    /// Повторный end по уже завершённой сессии идемпотентен: звонка больше нет.
    func test_end_invalidTransitionFromTerminalState_isEnded() {
        let already: [String: Any] = ["ok": false, "reason": "invalid_state_transition", "current_state": "completed"]
        XCTAssertEqual(CallEndOutcome.parse(["result": already]), .ended)
        let live: [String: Any] = ["ok": false, "reason": "invalid_state_transition", "current_state": "talking"]
        guard case .failed = CallEndOutcome.parse(["result": live]) else {
            return XCTFail("переход отвергнут из живого состояния: звонок мог остаться живым")
        }
    }

    func test_failedEndNeverRestoresAnAlreadyEndedSession() {
        XCTAssertFalse(CallEndOutcome.shouldRestoreAfterFailedEnd(sessionAlreadyEnded: true))
        XCTAssertTrue(CallEndOutcome.shouldRestoreAfterFailedEnd(sessionAlreadyEnded: false))
    }

    func test_controllerGuardsEndCompletionsWithTheRestoreRule() throws {
        var root = URL(fileURLWithPath: #file)
        for _ in 0..<6 where !FileManager.default.fileExists(
            atPath: root.appendingPathComponent("Sources/KrabEarAgent").path) { root.deleteLastPathComponent() }
        let text = try String(
            contentsOf: root.appendingPathComponent("Sources/KrabEarAgent/CallAutomationController.swift"),
            encoding: .utf8)
        let uses = text.components(separatedBy: "shouldRestoreAfterFailedEnd").count - 1
        XCTAssertGreaterThanOrEqual(uses, 2, "оба обработчика (hangup и emergency stop) обязаны сверяться с правилом")
    }
}
