/*
 ConversationInterruptTests — Волна 3c.

 Покрывает:
 1. Декодирование conv.interrupted → .interrupted(reason:) (было .unknown — событие молча логировалось).
 2. handleDownlinkEvent(.interrupted) — стоп плеера-хвоста, state → .listening, строка «— Прервано».
 3. interruptAI() ждёт серверного подтверждения; 2с fallback переводит локально.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Task 1: decode

final class ConversationInterruptedDecodeTests: XCTestCase {

    private func decode(_ json: String) -> ConversationEvent? {
        ConversationEvent.decode(from: Data(json.utf8))
    }

    func test_decode_interrupted_withReason() {
        let ev = decode(#"{"type":"conv.interrupted","data":{"reason":"user_started_speaking"}}"#)
        guard case .interrupted(let reason)? = ev else {
            return XCTFail("Ожидали .interrupted, получили \(String(describing: ev))")
        }
        XCTAssertEqual(reason, "user_started_speaking")
    }

    func test_decode_interrupted_withoutReason_emptyString() {
        let ev = decode(#"{"type":"conv.interrupted"}"#)
        guard case .interrupted(let reason)? = ev else {
            return XCTFail("Ожидали .interrupted, получили \(String(describing: ev))")
        }
        XCTAssertEqual(reason, "")
    }
}
