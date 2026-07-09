/*
 ConversationEventsTests — XCTest suite для ConversationEvents.swift.

 Покрывает:
 - Codable roundtrip для ConversationControlMessage / ConversationControlAction
 - Декодирование conv.* событий (реальный словарь Voice Gateway, 2026-06-20)
 - Чтение контента из подобъекта "data" (НЕ с верхнего уровня — это был баг)
 - Обратную совместимость со старым словарём (stt.partial / engine.loaded / error)
 - Обработку отсутствующих и некорректных полей
 - rawValue wire-формат для ConversationControlAction
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

    // MARK: - conv.transcript_partial (Voice Gateway — реальный словарь)

    func test_convTranscriptPartial_readsTextFromData() {
        // VERIFIED payload shape from live VG session 2026-06-20
        let event = decode([
            "type": "conv.transcript_partial",
            "ts": 1718880000,
            "session_id": "vs_abc123",
            "data": ["text": "Привет"] as [String: Any]
        ])
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            return XCTFail("Expected sttPartial, got \(String(describing: event))")
        }
        XCTAssertEqual(text, "Привет")
        XCTAssertEqual(lang, "")
        XCTAssertFalse(isFinal)
    }

    func test_convTranscriptPartial_missingData_textIsEmpty() {
        let event = decode(["type": "conv.transcript_partial"])
        guard case .sttPartial(let text, _, let isFinal) = event else {
            return XCTFail("Expected sttPartial")
        }
        XCTAssertEqual(text, "")
        XCTAssertFalse(isFinal)
    }

    // MARK: - conv.transcript_final (Voice Gateway — реальный словарь)

    func test_convTranscriptFinal_readsTextFromData_isFinalTrue() {
        // VERIFIED payload: {"type":"conv.transcript_final","data":{"text":"Привет, ..."}}
        let event = decode([
            "type": "conv.transcript_final",
            "ts": 1718880001,
            "session_id": "vs_abc123",
            "data": ["text": "Привет, как дела?"] as [String: Any]
        ])
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            return XCTFail("Expected sttPartial(isFinal=true), got \(String(describing: event))")
        }
        XCTAssertEqual(text, "Привет, как дела?")
        XCTAssertEqual(lang, "")
        XCTAssertTrue(isFinal, "conv.transcript_final должен давать isFinal=true")
    }

    func test_convTranscriptFinal_doesNotReadTextFromTopLevel() {
        // Баг: старый декодер читал raw["text"], а не data["text"].
        // Убеждаемся, что text берётся из data, а не из top-level.
        let event = decode([
            "type": "conv.transcript_final",
            "text": "WRONG_TOP_LEVEL",
            "data": ["text": "CORRECT_DATA"] as [String: Any]
        ])
        guard case .sttPartial(let text, _, _) = event else {
            return XCTFail("Expected sttPartial")
        }
        XCTAssertEqual(text, "CORRECT_DATA", "Контент должен читаться из data, не top-level")
    }

    // MARK: - conv.reply_final (Voice Gateway — реальный словарь)

    func test_convReplyFinal_readsTextFromData() {
        // VERIFIED payload: {"type":"conv.reply_final","data":{"text":"Привет! В Москве ..."}}
        let event = decode([
            "type": "conv.reply_final",
            "ts": 1718880002,
            "session_id": "vs_abc123",
            "data": ["text": "Привет! В Москве сейчас солнечно."] as [String: Any]
        ])
        guard case .replyFinal(let text) = event else {
            return XCTFail("Expected replyFinal, got \(String(describing: event))")
        }
        XCTAssertEqual(text, "Привет! В Москве сейчас солнечно.")
    }

    func test_convReplyFinal_missingData_textIsEmpty() {
        let event = decode(["type": "conv.reply_final"])
        guard case .replyFinal(let text) = event else {
            return XCTFail("Expected replyFinal")
        }
        XCTAssertEqual(text, "")
    }

    func test_convReplyFinal_doesNotReadTextFromTopLevel() {
        let event = decode([
            "type": "conv.reply_final",
            "text": "WRONG",
            "data": ["text": "CORRECT"] as [String: Any]
        ])
        guard case .replyFinal(let text) = event else {
            return XCTFail("Expected replyFinal")
        }
        XCTAssertEqual(text, "CORRECT")
    }

    // MARK: - conv.ready (Voice Gateway — реальный словарь)

    func test_convReady_readsEngineNameFromData() {
        // VERIFIED payload: {"type":"conv.ready","data":{"engine":"krab_ear_pipeline","sample_rate":16000,...}}
        let event = decode([
            "type": "conv.ready",
            "ts": 1718880000,
            "session_id": "vs_abc123",
            "data": [
                "engine": "krab_ear_pipeline",
                "sample_rate": 16000
            ] as [String: Any]
        ])
        guard case .engineLoaded(let name, let elapsed) = event else {
            return XCTFail("Expected engineLoaded, got \(String(describing: event))")
        }
        XCTAssertEqual(name, "krab_ear_pipeline")
        XCTAssertEqual(elapsed, 0.0, accuracy: 1e-9)
    }

    func test_convReady_missingEngine_nameIsEmpty() {
        let event = decode(["type": "conv.ready", "data": [:] as [String: Any]])
        guard case .engineLoaded(let name, _) = event else {
            return XCTFail("Expected engineLoaded")
        }
        XCTAssertEqual(name, "")
    }

    // MARK: - conv.error / conv.fatal

    func test_convError_codeIsConvError() {
        let event = decode([
            "type": "conv.error",
            "data": ["message": "STT timeout"] as [String: Any]
        ])
        guard case .error(let code, let message) = event else {
            return XCTFail("Expected error, got \(String(describing: event))")
        }
        XCTAssertEqual(code, "conv.error")
        XCTAssertEqual(message, "STT timeout")
    }

    func test_convFatal_codeIsConvFatal() {
        let event = decode([
            "type": "conv.fatal",
            "data": ["error": "GPU out of memory"] as [String: Any]
        ])
        guard case .error(let code, let message) = event else {
            return XCTFail("Expected error, got \(String(describing: event))")
        }
        XCTAssertEqual(code, "conv.fatal")
        XCTAssertEqual(message, "GPU out of memory")
    }

    func test_convError_missingMessage_defaultsToEmpty() {
        let event = decode(["type": "conv.error"])
        guard case .error(let code, let message) = event else {
            return XCTFail("Expected error")
        }
        XCTAssertEqual(code, "conv.error")
        XCTAssertEqual(message, "")
    }

    // MARK: - conv.recycled

    func test_convRecycled_returnsRecycledCase() {
        let event = decode([
            "type": "conv.recycled",
            "data": ["reason": "5min_cap", "recycled_count": 1] as [String: Any]
        ])
        guard case .recycled(let reason) = event else {
            return XCTFail("Expected recycled, got \(String(describing: event))")
        }
        XCTAssertEqual(reason, "5min_cap")
    }

    // MARK: - Graceful handling of conv.interrupted, conv.vad_*, conv.audio_chunk

    func test_convClosed_returnsClosed() {
        let event = decode(["type": "conv.closed"])
        guard case .closed = event else {
            return XCTFail("Expected .closed for conv.closed")
        }
    }

    func test_convInterrupted_returnsInterrupted() {
        // Волна 3c: conv.interrupted больше не .unknown — типизированное событие
        // с reason (см. ConversationInterruptedDecodeTests для полного покрытия).
        let event = decode(["type": "conv.interrupted"])
        guard case .interrupted(let reason) = event else {
            return XCTFail("Expected .interrupted for conv.interrupted, got \(String(describing: event))")
        }
        XCTAssertEqual(reason, "")
    }

    func test_convVadSpeech_returnsUnknown() {
        let event = decode(["type": "conv.vad_speech"])
        guard case .unknown = event else {
            return XCTFail("Expected unknown for conv.vad_speech")
        }
    }

    func test_convVadSilence_returnsUnknown() {
        let event = decode(["type": "conv.vad_silence"])
        guard case .unknown = event else {
            return XCTFail("Expected unknown for conv.vad_silence")
        }
    }

    func test_convAudioChunk_returnsUnknown() {
        let event = decode(["type": "conv.audio_chunk", "data": ["bytes": "base64..."] as [String: Any]])
        guard case .unknown = event else {
            return XCTFail("Expected unknown for conv.audio_chunk")
        }
    }

    // MARK: - Обратная совместимость: старый словарь stt.partial / engine.loaded / error

    func test_legacy_sttPartial_fullPayload_parsesCorrectly() {
        let event = decode(["type": "stt.partial", "text": "Привет", "lang": "ru", "is_final": true])
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            return XCTFail("Expected sttPartial, got \(String(describing: event))")
        }
        XCTAssertEqual(text, "Привет")
        XCTAssertEqual(lang, "ru")
        XCTAssertTrue(isFinal)
    }

    func test_legacy_sttPartial_missingFields_usesDefaults() {
        let event = decode(["type": "stt.partial"])
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            return XCTFail("Expected sttPartial")
        }
        XCTAssertEqual(text, "")
        XCTAssertEqual(lang, "")
        XCTAssertFalse(isFinal)
    }

    func test_legacy_engineLoaded_parsesNameAndElapsedSec() {
        let event = decode(["type": "engine.loaded", "name": "moshi-7b", "elapsed_sec": 3.14])
        guard case .engineLoaded(let name, let elapsedSec) = event else {
            return XCTFail("Expected engineLoaded, got \(String(describing: event))")
        }
        XCTAssertEqual(name, "moshi-7b")
        XCTAssertEqual(elapsedSec, 3.14, accuracy: 1e-6)
    }

    func test_legacy_engineLoaded_missingFields_usesDefaults() {
        let event = decode(["type": "engine.loaded"])
        guard case .engineLoaded(let name, let elapsedSec) = event else {
            return XCTFail("Expected engineLoaded")
        }
        XCTAssertEqual(name, "")
        XCTAssertEqual(elapsedSec, 0.0, accuracy: 1e-9)
    }

    func test_legacy_error_parsesCodeAndMessage() {
        let event = decode(["type": "error", "code": "auth_failed", "message": "Token expired"])
        guard case .error(let code, let message) = event else {
            return XCTFail("Expected error, got \(String(describing: event))")
        }
        XCTAssertEqual(code, "auth_failed")
        XCTAssertEqual(message, "Token expired")
    }

    func test_legacy_error_missingCode_defaultsToUnknown() {
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
