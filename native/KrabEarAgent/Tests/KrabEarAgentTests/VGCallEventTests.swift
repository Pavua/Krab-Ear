import XCTest
@testable import KrabEarAgent

final class VGCallEventTests: XCTestCase {
    private func decode(_ json: String) -> VGCallEvent? {
        VGCallEvent.decode(json.data(using: .utf8)!)
    }

    func test_sttFinal_full_shape() {
        let e = decode(#"{"type":"stt.final","ts":"2026-08-21T10:00:00Z","data":{"text":"hola","engine":"gigaam","confidence":0.91,"duration_ms":900,"language":"es"}}"#)
        XCTAssertEqual(e, .sttFinal(text: "hola", language: "es", confidence: 0.91))
    }

    func test_sttFinal_takeover_shape_text_language_only() {
        let e = decode(#"{"type":"stt.final","ts":"t","data":{"text":"si","language":"es"}}"#)
        XCTAssertEqual(e, .sttFinal(text: "si", language: "es", confidence: nil))
    }

    func test_sttFinal_realtime_shape_text_only() {
        let e = decode(#"{"type":"stt.final","ts":"t","data":{"text":"ok"}}"#)
        XCTAssertEqual(e, .sttFinal(text: "ok", language: nil, confidence: nil))
    }

    func test_translationFinal_with_provider() {
        let e = decode(#"{"type":"translation.final","ts":"t","data":{"text":"привет","source_text":"hola","src_lang":"es","tgt_lang":"ru","provider":"argos"}}"#)
        XCTAssertEqual(e, .translationFinal(text: "привет", sourceText: "hola", srcLang: "es", tgtLang: "ru"))
    }

    func test_agentResponse_full_and_minimal_realtime() {
        let full = decode(#"{"type":"agent.response","ts":"t","data":{"text":"Claro","text_ru":"Конечно","action":"continue","goal_reached":false,"summary":"","role":"assistant","lang":"es","utterance_ts":"u1"}}"#)
        XCTAssertEqual(full, .agentResponse(text: "Claro", textRu: "Конечно", utteranceTs: "u1", action: "continue"))
        let minimal = decode(#"{"type":"agent.response","ts":"t","data":{"text":"Si","utterance_ts":"u2","role":"assistant","lang":"es"}}"#)
        XCTAssertEqual(minimal, .agentResponse(text: "Si", textRu: nil, utteranceTs: "u2", action: nil))
    }

    func test_agentAutoSpoken() {
        let e = decode(#"{"type":"agent.suggestion.auto_spoken","ts":"t","data":{"text":"Uno","text_ru":"Один","action":"dtmf","digits":"1","goal_reached":false,"summary":"","result":""}}"#)
        XCTAssertEqual(e, .agentAutoSpoken(text: "Uno", textRu: "Один", action: "dtmf", digits: "1"))
    }

    func test_agentInterrupted_spoken_prefix() {
        let e = decode(#"{"type":"agent.interrupted","ts":"t","data":{"utterance_ts":"u1","spoken_fraction":0.42,"spoken_text":"Claro, ahora"}}"#)
        XCTAssertEqual(e, .agentInterrupted(utteranceTs: "u1", spokenFraction: 0.42, spokenText: "Claro, ahora"))
    }

    func test_callState_with_and_without_mute_hold() {
        XCTAssertEqual(decode(#"{"type":"call.state","ts":"t","data":{"session_id":"s","status":"running"}}"#),
                       .callState(status: "running", muted: nil, held: nil))
        XCTAssertEqual(decode(#"{"type":"call.state","ts":"t","data":{"status":"paused","muted":false,"held":true}}"#),
                       .callState(status: "paused", muted: false, held: true))
    }

    func test_callRinging_has_no_status_field() {
        XCTAssertEqual(decode(#"{"type":"call.ringing","ts":"t","data":{"call_sid":"CA1","twilio_status":"ringing","provider":"twilio"}}"#), .callRinging)
    }

    func test_callEnded_webhook_optional_fields() {
        XCTAssertEqual(decode(#"{"type":"call.ended","ts":"t","data":{"reason":"hangup","provider":"twilio","call_sid":"CA1","duration_seconds":63,"twilio_status":"completed"}}"#),
                       .callEnded(reason: "hangup"))
    }

    func test_callClosed() {
        XCTAssertEqual(decode(#"{"type":"call.closed","ts":"t","data":{"session_id":"s"}}"#), .callClosed)
    }

    func test_costAlert() {
        XCTAssertEqual(decode(#"{"type":"cost.alert","ts":"t","data":{"level":"session","threshold_usd":1.0,"current_usd":1.05,"message":"m"}}"#),
                       .costAlert(level: "session", currentUsd: 1.05, message: "m"))
    }

    func test_exact_match_prefix_trap() {
        // Игнорируемый agent.suggestion НЕ должен матчиться как auto_spoken.
        XCTAssertEqual(decode(#"{"type":"agent.suggestion","ts":"t","data":{"text":"x"}}"#), .ignored(type: "agent.suggestion"))
    }

    func test_unknown_type_ignored_and_pong_without_ts() {
        XCTAssertEqual(decode(#"{"type":"diagnostic.status","ts":"t","data":{}}"#), .ignored(type: "diagnostic.status"))
        XCTAssertEqual(decode(#"{"type":"pong"}"#), .ignored(type: "pong"))
    }

    func test_malformed_returns_nil() {
        XCTAssertNil(decode("not json"))
        XCTAssertNil(decode(#"{"no_type":1}"#))
    }
}
