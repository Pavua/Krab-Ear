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

// MARK: - Task 2: handleInterrupted + interruptAI fallback

@MainActor
final class ConversationInterruptHandlingTests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
        vc.isSessionActive = true
    }

    override func tearDown() async throws {
        vc.interruptFallbackTimer?.invalidate()
        vc = nil
        try await super.tearDown()
    }

    func test_interruptedEvent_setsListening_andAppendsTranscriptLine() {
        vc.conversationState = .speaking
        vc.handleDownlinkEvent(.interrupted(reason: "user_started_speaking"))
        XCTAssertEqual(vc.conversationState, .listening)
        XCTAssertTrue(vc.transcriptBuffer.contains("— Прервано"),
                      "transcript должен получить служебную строку «— Прервано»")
    }

    func test_interruptedEvent_ignored_whenSessionInactive() {
        vc.isSessionActive = false
        vc.conversationState = .idle
        vc.handleDownlinkEvent(.interrupted(reason: "x"))
        XCTAssertEqual(vc.conversationState, .idle)
        XCTAssertFalse(vc.transcriptBuffer.contains("— Прервано"))
    }

    func test_interruptAI_doesNotSwitchStateImmediately() {
        vc.conversationState = .speaking
        vc.interruptAI()
        XCTAssertEqual(vc.conversationState, .speaking,
                       "interruptAI ждёт серверного conv.interrupted, не переключает сам")
        XCTAssertNotNil(vc.interruptFallbackTimer, "fallback-таймер должен быть взведён")
    }

    func test_interruptAI_fallbackFires_whenNoServerConfirmation() {
        vc.interruptFallbackInterval = 0.05
        vc.conversationState = .speaking
        vc.interruptAI()
        let exp = expectation(description: "fallback")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exp.fulfill() }
        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(vc.conversationState, .listening)
        XCTAssertTrue(vc.transcriptBuffer.contains("— Прервано"))
    }

    func test_serverConfirmation_cancelsFallback_noDoubleLine() {
        vc.interruptFallbackInterval = 0.05
        vc.conversationState = .speaking
        vc.interruptAI()
        vc.handleDownlinkEvent(.interrupted(reason: "confirmed"))  // подтверждение пришло раньше fallback
        let exp = expectation(description: "wait past fallback window")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exp.fulfill() }
        wait(for: [exp], timeout: 2.0)
        let occurrences = vc.transcriptBuffer.components(separatedBy: "— Прервано").count - 1
        XCTAssertEqual(occurrences, 1, "fallback не должен продублировать обработку")
    }

    func test_flushDownlinkPlayback_nilPlayer_noCrash() {
        // Аудио-движок не стартовал — playerNode nil; вызов не должен падать.
        vc.flushDownlinkPlayback()
    }
}
