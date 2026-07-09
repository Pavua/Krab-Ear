/*
 ConversationErrorAnnouncerTests — Волна 3c, секция «локальная озвучка ошибок».

 Дебаунс 30с на класс ошибки; фразы фиксированы спекой и НЕ содержат слово
 «Краб» (анти-триггер wake word); отсутствие speak-клоужера — тихая деградация.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class ConversationErrorAnnouncerTests: XCTestCase {

    private var announcer: ConversationErrorAnnouncer!
    private var spoken: [String] = []
    private var fakeNow: Date = Date(timeIntervalSince1970: 1_000_000)

    override func setUp() async throws {
        try await super.setUp()
        spoken = []
        fakeNow = Date(timeIntervalSince1970: 1_000_000)
        announcer = ConversationErrorAnnouncer()
        announcer.now = { [weak self] in self?.fakeNow ?? Date() }
        announcer.speak = { [weak self] phrase in self?.spoken.append(phrase) }
    }

    func test_firstAnnounce_speaksPhrase() {
        XCTAssertTrue(announcer.announce(.gatewayUnreachable))
        XCTAssertEqual(spoken, ["Голосовой шлюз недоступен."])
    }

    func test_debounce_blocksRepeatWithin30s() {
        _ = announcer.announce(.connectionLost)
        fakeNow = fakeNow.addingTimeInterval(29)
        XCTAssertFalse(announcer.announce(.connectionLost))
        XCTAssertEqual(spoken.count, 1)
    }

    func test_debounce_allowsAfter30s() {
        _ = announcer.announce(.connectionLost)
        fakeNow = fakeNow.addingTimeInterval(31)
        XCTAssertTrue(announcer.announce(.connectionLost))
        XCTAssertEqual(spoken.count, 2)
    }

    func test_debounce_isPerClass_independentClasses() {
        _ = announcer.announce(.gatewayUnreachable)
        XCTAssertTrue(announcer.announce(.serverError),
                      "дебаунс per-class: другой класс не блокируется")
        XCTAssertEqual(spoken, ["Голосовой шлюз недоступен.", "Произошла ошибка. Попробуй ещё раз."])
    }

    func test_noSpeakClosure_returnsFalse_noCrash() {
        announcer.speak = nil
        XCTAssertFalse(announcer.announce(.serverError))
    }

    func test_phrases_doNotContainWakeWord() {
        for phrase in ConversationErrorAnnouncer.phrases.values {
            XCTAssertFalse(phrase.lowercased().contains("краб"),
                           "фраза «\(phrase)» не должна содержать wake word")
        }
    }
}
