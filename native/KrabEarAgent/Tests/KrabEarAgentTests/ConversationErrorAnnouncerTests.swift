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
        XCTAssertTrue(src.contains("classifyAndAnnounceWSFailure(httpStatus: status)"),
                      "startReceiveLoop failure-ветка должна вызывать classifyAndAnnounceWSFailure(httpStatus:)")
    }

    func test_sourceContract_receiveLoopSuccessBranch_gatedByGenerationAndTask() throws {
        // Успешная ветка обязана проверять UUID сессии и идентичность сокет-задачи.
        // Одного isSessionActive недостаточно: после быстрого stop→start старый
        // callback увидит уже активную новую сессию.
        let src = try String(contentsOf: Self.wsSwiftURL, encoding: .utf8)
        guard let successRange = src.range(of: "case .success(let message):"),
              let failureRange = src.range(of: "case .failure(let error):"),
              successRange.lowerBound < failureRange.lowerBound
        else {
            return XCTFail("Не нашли ветки .success/.failure в startReceiveLoop")
        }
        let successBranch = src[successRange.upperBound..<failureRange.lowerBound]
        XCTAssertTrue(successBranch.contains("acceptsConversationCallback(generation)"),
                      "Успешная ветка должна проверять поколение разговора")
        XCTAssertTrue(successBranch.contains("wsHolder.task === task"),
                      "Успешная ветка должна оставаться привязана к исходной задаче")
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

// MARK: - 401/403 при рукопожатии WS: «нужен ключ», а не «шлюз недоступен»

@MainActor
final class ConversationGatewayAuthFailureTests: XCTestCase {

    func test_failureClass_401and403_isAuthRequired_regardlessOfState() {
        for status in [401, 403] {
            for state: ConversationState in [.connecting, .listening] {
                XCTAssertEqual(
                    ConversationViewController.failureClass(httpStatus: status, state: state),
                    .gatewayAuthRequired, "status \(status), state \(state)")
            }
        }
    }

    func test_failureClass_otherStatusesKeepOldBehaviour() {
        for status: Int? in [nil, 200, 404, 500, 502] {
            XCTAssertEqual(ConversationViewController.failureClass(httpStatus: status, state: .connecting),
                           .gatewayUnreachable, "status \(String(describing: status))")
            XCTAssertEqual(ConversationViewController.failureClass(httpStatus: status, state: .listening),
                           .connectionLost, "status \(String(describing: status))")
        }
    }

    func test_authPhrase_namesTheKeyAndHasNoWakeWord() throws {
        let phrase = try XCTUnwrap(ConversationErrorAnnouncer.phrases[.gatewayAuthRequired])
        XCTAssertTrue(phrase.lowercased().contains("ключ"))
        XCTAssertFalse(phrase.lowercased().contains("краб"))
    }

    func test_authDebounce_isMuchLongerThanOtherClasses() {
        let announcer = ConversationErrorAnnouncer()
        var spoken: [String] = []
        var now = Date(timeIntervalSince1970: 1_000_000)
        announcer.now = { now }
        announcer.speak = { spoken.append($0) }
        XCTAssertTrue(announcer.announce(.gatewayAuthRequired))
        now = now.addingTimeInterval(120)      // ошибки соединения за это время уже повторились бы
        XCTAssertFalse(announcer.announce(.gatewayAuthRequired), "ошибка конфигурации не должна повторяться голосом каждые 30 с")
        now = now.addingTimeInterval(ConversationErrorAnnouncer.authDebounceInterval)
        XCTAssertTrue(announcer.announce(.gatewayAuthRequired))
        XCTAssertEqual(spoken.count, 2)
    }

    func test_wiring_authFailure_announcesKeyPhrase_and_showsActionableError() {
        let vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
        var spoken: [String] = []
        vc.errorAnnouncer.speak = { spoken.append($0) }
        vc.isSessionActive = true
        vc.conversationState = .connecting
        vc.classifyAndAnnounceWSFailure(httpStatus: 401)
        XCTAssertEqual(spoken.count, 1)
        XCTAssertTrue(spoken[0].lowercased().contains("ключ"), spoken[0])
        XCTAssertTrue(ConversationViewController.failureDescription(httpStatus: 401, fallback: "bad response")
            .contains("Live перевод"))
        XCTAssertEqual(ConversationViewController.failureDescription(httpStatus: 500, fallback: "bad response"),
                       "bad response")
        XCTAssertEqual(ConversationViewController.failureDescription(httpStatus: nil, fallback: "x"), "x")
    }

    func test_sourceContract_receiveFailureReadsHandshakeStatus() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/ConversationViewController+WebSocket.swift")
        let src = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(src.contains("(task.response as? HTTPURLResponse)?.statusCode"),
                      "receive-failure должна читать HTTP-статус рукопожатия")
        XCTAssertTrue(src.contains("classifyAndAnnounceWSFailure(httpStatus: status)"))
        XCTAssertTrue(src.contains("failureDescription(httpStatus: status, fallback: desc)"))
    }
}
