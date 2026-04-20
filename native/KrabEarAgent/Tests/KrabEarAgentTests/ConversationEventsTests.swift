/*
 ConversationEventsTests — XCTest suite для ConversationEvents.swift (Phase 1 VA).

 Покрывает:
 - Codable roundtrip для ConversationControlMessage / ConversationControlAction
 - Декодирование каждого downlink event type из JSON
 - Обработку отсутствующих и некорректных полей
 - rawValue wire-формат для ConversationControlAction
 - Вложенные payload (args) у tool.invoked
 - Возврат nil при невалидном JSON
*/

import XCTest
@testable import KrabEarAgent

final class ConversationEventsTests: XCTestCase {

    // MARK: - Helpers

    private func json(_ dict: [String: Any]) -> Data {
        try! JSONSerialization.data(withJSONObject: dict)
    }

    private func decode(_ dict: [String: Any]) -> ConversationEvent? {
        ConversationEvent.decode(from: json(dict))
    }

    private func encodeControl(_ action: ConversationControlAction) -> [String: Any] {
        let msg = ConversationControlMessage(action: action)
        let data = msg.jsonData!
        return try! JSONSerialization.jsonObject(with: data) as! [String: Any]
    }

    // MARK: - ConversationControlAction rawValue (wire format)

    func test_controlAction_rawValues_matchWireFormat() {
        XCTAssertEqual(ConversationControlAction.interrupt.rawValue, "interrupt")
        XCTAssertEqual(ConversationControlAction.end.rawValue, "end")
        XCTAssertEqual(ConversationControlAction.pushToTalkOff.rawValue, "push_to_talk_off")
    }

    // MARK: - ConversationControlMessage Encodable roundtrip

    func test_controlMessage_interrupt_encodesCorrectly() {
        let encoded = encodeControl(.interrupt)
        XCTAssertEqual(encoded["type"] as? String, "control")
        XCTAssertEqual(encoded["action"] as? String, "interrupt")
    }

    func test_controlMessage_end_encodesCorrectly() {
        let encoded = encodeControl(.end)
        XCTAssertEqual(encoded["type"] as? String, "control")
        XCTAssertEqual(encoded["action"] as? String, "end")
    }

    func test_controlMessage_pushToTalkOff_encodesCorrectly() {
        let encoded = encodeControl(.pushToTalkOff)
        XCTAssertEqual(encoded["type"] as? String, "control")
        XCTAssertEqual(encoded["action"] as? String, "push_to_talk_off")
    }

    func test_controlMessage_jsonData_isNotNil() {
        let msg = ConversationControlMessage(action: .end)
        XCTAssertNotNil(msg.jsonData)
    }

    // MARK: - stt.partial downlink

    func test_sttPartial_fullPayload_parsesCorrectly() {
        let event = decode(["type": "stt.partial", "text": "Привет", "lang": "ru", "is_final": true])
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            return XCTFail("Expected sttPartial, got \(String(describing: event))")
        }
        XCTAssertEqual(text, "Привет")
        XCTAssertEqual(lang, "ru")
        XCTAssertTrue(isFinal)
    }

    func test_sttPartial_missingFields_usesDefaults() {
        // Only type present — all optional fields fall back to defaults
        let event = decode(["type": "stt.partial"])
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            return XCTFail("Expected sttPartial")
        }
        XCTAssertEqual(text, "")
        XCTAssertEqual(lang, "")
        XCTAssertFalse(isFinal)
    }

    func test_sttPartial_isFinalFalse_parsesCorrectly() {
        let event = decode(["type": "stt.partial", "text": "Hola", "lang": "es", "is_final": false])
        guard case .sttPartial(_, _, let isFinal) = event else {
            return XCTFail("Expected sttPartial")
        }
        XCTAssertFalse(isFinal)
    }

    // MARK: - engine.loaded downlink

    func test_engineLoaded_parsesNameAndElapsedSec() {
        let event = decode(["type": "engine.loaded", "name": "moshi-7b", "elapsed_sec": 3.14])
        guard case .engineLoaded(let name, let elapsedSec) = event else {
            return XCTFail("Expected engineLoaded, got \(String(describing: event))")
        }
        XCTAssertEqual(name, "moshi-7b")
        XCTAssertEqual(elapsedSec, 3.14, accuracy: 1e-6)
    }

    func test_engineLoaded_missingFields_usesDefaults() {
        let event = decode(["type": "engine.loaded"])
        guard case .engineLoaded(let name, let elapsedSec) = event else {
            return XCTFail("Expected engineLoaded")
        }
        XCTAssertEqual(name, "")
        XCTAssertEqual(elapsedSec, 0.0, accuracy: 1e-9)
    }

    // MARK: - tool.invoked downlink

    func test_toolInvoked_parsesToolAndArgs() {
        let event = decode([
            "type": "tool.invoked",
            "tool": "web_search",
            "args": ["query": "swift codable", "limit": 5] as [String: Any]
        ])
        guard case .toolInvoked(let tool, let args) = event else {
            return XCTFail("Expected toolInvoked, got \(String(describing: event))")
        }
        XCTAssertEqual(tool, "web_search")
        XCTAssertEqual(args["query"] as? String, "swift codable")
        XCTAssertEqual(args["limit"] as? Int, 5)
    }

    func test_toolInvoked_emptyArgs_parsesCorrectly() {
        let event = decode(["type": "tool.invoked", "tool": "noop"])
        guard case .toolInvoked(let tool, let args) = event else {
            return XCTFail("Expected toolInvoked")
        }
        XCTAssertEqual(tool, "noop")
        XCTAssertTrue(args.isEmpty)
    }

    // MARK: - summary.ready downlink

    func test_summaryReady_parsesTextAndLang() {
        let event = decode(["type": "summary.ready", "text": "Call ended.", "lang": "en"])
        guard case .summaryReady(let text, let lang) = event else {
            return XCTFail("Expected summaryReady, got \(String(describing: event))")
        }
        XCTAssertEqual(text, "Call ended.")
        XCTAssertEqual(lang, "en")
    }

    func test_summaryReady_missingFields_usesDefaults() {
        let event = decode(["type": "summary.ready"])
        guard case .summaryReady(let text, let lang) = event else {
            return XCTFail("Expected summaryReady")
        }
        XCTAssertEqual(text, "")
        XCTAssertEqual(lang, "")
    }

    // MARK: - error downlink

    func test_error_parsesCodeAndMessage() {
        let event = decode(["type": "error", "code": "auth_failed", "message": "Token expired"])
        guard case .error(let code, let message) = event else {
            return XCTFail("Expected error, got \(String(describing: event))")
        }
        XCTAssertEqual(code, "auth_failed")
        XCTAssertEqual(message, "Token expired")
    }

    func test_error_missingCode_defaultsToUnknown() {
        let event = decode(["type": "error", "message": "Oops"])
        guard case .error(let code, _) = event else {
            return XCTFail("Expected error")
        }
        XCTAssertEqual(code, "unknown")
    }

    // MARK: - unknown / forward-compat

    func test_unknownType_returnsUnknownCase() {
        let event = decode(["type": "future.event", "payload": "xyz"])
        guard case .unknown(let type, let raw) = event else {
            return XCTFail("Expected unknown, got \(String(describing: event))")
        }
        XCTAssertEqual(type, "future.event")
        XCTAssertEqual(raw["payload"] as? String, "xyz")
    }

    // MARK: - Invalid JSON

    func test_invalidJSON_returnsNil() {
        let bad = "not json at all".data(using: .utf8)!
        XCTAssertNil(ConversationEvent.decode(from: bad))
    }

    func test_missingTypeField_returnsNil() {
        let event = decode(["text": "no type here"])
        XCTAssertNil(event)
    }

    func test_emptyObject_returnsNil() {
        let event = decode([:])
        XCTAssertNil(event)
    }
}
