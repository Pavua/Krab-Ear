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

// MARK: - Task 4: проводка триггеров в ConversationViewController

@MainActor
final class ConversationErrorAnnouncerWiringTests: XCTestCase {

    private var vc: ConversationViewController!
    private var spoken: [String] = []

    override func setUp() async throws {
        try await super.setUp()
        spoken = []
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
        vc.errorAnnouncer.speak = { [weak self] phrase in self?.spoken.append(phrase) }
        vc.isSessionActive = true
    }

    override func tearDown() async throws {
        vc.interruptFallbackTimer?.invalidate()
        vc = nil
        try await super.tearDown()
    }

    func test_wsFailure_whileConnecting_announcesGatewayUnreachable() {
        vc.conversationState = .connecting
        vc.classifyAndAnnounceWSFailure()
        XCTAssertEqual(spoken, ["Голосовой шлюз недоступен."])
    }

    func test_wsFailure_midSession_announcesConnectionLost() {
        vc.conversationState = .listening
        vc.classifyAndAnnounceWSFailure()
        XCTAssertEqual(spoken, ["Связь с голосовым шлюзом потеряна."])
    }

    func test_convError_announcesServerError() {
        vc.handleDownlinkEvent(.error(code: "conv.error", message: "brain exploded"))
        XCTAssertEqual(spoken, ["Произошла ошибка. Попробуй ещё раз."])
    }

    func test_userStop_neverAnnounces() {
        vc.conversationState = .listening
        vc.stopConversation()
        XCTAssertTrue(spoken.isEmpty, "штатная остановка пользователем не озвучивается")
    }

    func test_convError_afterUserStop_notAnnounced_noErrorState() {
        // Гонка: conv.error уже в полёте (receive-callback принял байты) в момент,
        // когда юзер жмёт «Стоп» — Task из callback встаёт в очередь MainActor
        // ПОСЛЕ stopConversation(). Инвариант спеки: юзерская остановка НЕ
        // озвучивается никогда; UI не должен застрять в «Ошибка» вместо «Готов».
        vc.isSessionActive = false  // stopConversation() уже отработал
        vc.conversationState = .idle
        vc.handleDownlinkEvent(.error(code: "conv.error", message: "boom"))
        XCTAssertTrue(spoken.isEmpty, "in-flight conv.error после юзерского Стоп не озвучивается")
        XCTAssertEqual(vc.conversationState, .idle,
                       "состояние не должно откатиться в .error после юзерского Стоп")
        XCTAssertFalse(vc.transcriptBuffer.contains("Ошибка"),
                       "transcript не должен пополниться строкой ошибки после Стоп")
    }

    func test_sourceContract_receiveLoopFailureBranch_callsClassifier() throws {
        // Receive-failure ветка в +WebSocket.swift обязана вызывать классификатор —
        // иначе озвучка «шлюз недоступен/связь потеряна» мертва в проде
        // (класс test-validates-the-hole: setupErrorBus/setupHealthMonitor).
        let src = try String(contentsOf: Self.wsSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains("classifyAndAnnounceWSFailure()"),
                      "startReceiveLoop failure-ветка должна вызывать classifyAndAnnounceWSFailure()")
    }

    func test_sourceContract_receiveLoopSuccessBranch_gatedByIsSessionActive() throws {
        // .success-ветка receive-цикла обязана гейтиться isSessionActive симметрично
        // .failure: без гейта in-flight событие, принятое в момент юзерского «Стоп»,
        // диспатчится в handleDownlinkEvent уже после остановки сессии.
        let src = try String(contentsOf: Self.wsSwiftURL, encoding: .utf8)
        guard let successRange = src.range(of: "case .success(let message):"),
              let failureRange = src.range(of: "case .failure(let error):"),
              successRange.lowerBound < failureRange.lowerBound
        else {
            return XCTFail("Не нашли ветки .success/.failure в startReceiveLoop")
        }
        let successBranch = src[successRange.upperBound..<failureRange.lowerBound]
        XCTAssertTrue(successBranch.contains("self.isSessionActive"),
                      ".success-ветка receive-цикла должна гейтиться isSessionActive")
    }

    private static var wsSwiftURL: URL {
        var url = URL(fileURLWithPath: #filePath)  // .../Tests/KrabEarAgentTests/<этот файл>
        url.deleteLastPathComponent()              // Tests/KrabEarAgentTests
        url.deleteLastPathComponent()              // Tests
        url.deleteLastPathComponent()              // native/KrabEarAgent
        return url
            .appendingPathComponent("Sources/KrabEarAgent/ConversationViewController+WebSocket.swift")
    }
}
