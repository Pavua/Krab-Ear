import AppKit
import XCTest
@testable import KrabEarAgent

/// Живая проверка владельца 25–26.11: разговор после «Краб» висел 4–5 часов и отвечал на ролики
/// и шум в комнате. Автозавершение по тишине тут бессильно: ролик — это «речь» и для локального
/// VAD, и для Voice Gateway, отсчёт тишины обнуляется вечно. Нужен жёсткий потолок длительности
/// сессии, не зависящий ни от состояния, ни от активности, ни от тумблера тишины.
@MainActor
final class ConversationHardCapTests: XCTestCase {

    func test_maxDurationSettings_defaultsAndBounds() {
        let defaults = UserDefaults(suiteName: "cap-\(UUID().uuidString)")!
        XCTAssertEqual(ConversationMaxDurationSettings.seconds(in: defaults), 600, "по умолчанию 10 минут")
        defaults.set(5, forKey: ConversationMaxDurationSettings.secondsKey)
        XCTAssertEqual(ConversationMaxDurationSettings.seconds(in: defaults), 120, "ниже минимума — 2 минуты")
        defaults.set(999_999, forKey: ConversationMaxDurationSettings.secondsKey)
        XCTAssertEqual(ConversationMaxDurationSettings.seconds(in: defaults), 3_600, "выше максимума — час")
        defaults.set(Double.nan, forKey: ConversationMaxDurationSettings.secondsKey)
        XCTAssertEqual(ConversationMaxDurationSettings.seconds(in: defaults), 600, "мусор — по умолчанию")
        defaults.set(-1, forKey: ConversationMaxDurationSettings.secondsKey)
        XCTAssertEqual(ConversationMaxDurationSettings.seconds(in: defaults), 600, "отрицательное — по умолчанию")
    }

    func test_maxDurationPolicy_expiresByElapsedUptimeOnly() {
        let policy = ConversationMaxDurationPolicy(limit: 600, startedAt: 100)
        XCTAssertFalse(policy.isExpired(now: 100))
        XCTAssertFalse(policy.isExpired(now: 699.9))
        XCTAssertTrue(policy.isExpired(now: 700))
        XCTAssertTrue(policy.isExpired(now: 10_000))
    }

    func test_maxDurationChoices_insideRange_andTitles() {
        XCTAssertTrue(ConversationMaxDurationSettings.choices.allSatisfy { ConversationMaxDurationSettings.range.contains($0) })
        XCTAssertTrue(ConversationMaxDurationSettings.choices.contains(ConversationMaxDurationSettings.defaultSeconds))
        XCTAssertEqual(ConversationMaxDurationSettings.title(600), "10 мин")
        XCTAssertEqual(ConversationMaxDurationSettings.title(3_600), "60 мин")
    }

    /// Потолок посреди ответа AI: даём договорить, но не дольше speakingGrace.
    func test_maxDurationPolicy_speakingGetsGrace_thenEnds() {
        let policy = ConversationMaxDurationPolicy(limit: 600, startedAt: 0)
        XCTAssertFalse(policy.isExpired(now: 601, state: .speaking), "договаривает")
        XCTAssertFalse(policy.isExpired(now: 659, state: .speaking))
        XCTAssertTrue(policy.isExpired(now: 660, state: .speaking), "запас кончился")
        XCTAssertTrue(policy.isExpired(now: 601, state: .listening))
        XCTAssertTrue(policy.isExpired(now: 601, state: .thinking))
    }

    private func makeVC(defaults: UserDefaults) -> ConversationViewController {
        let config = ConversationConfig(wsURLString: "ws://127.0.0.1:19999/v1/conversation", apiKey: "",
                                        languageHint: "auto", engine: "auto", brain: "auto")
        let vc = ConversationViewController(config: config, runtimeOptions: .isolatedTests, userDefaults: defaults)
        vc.loadView()
        vc.viewDidLoad()
        return vc
    }

    /// Шумная комната: каждые 5 с «голос» и реплики AI, отсчёт тишины обнуляется вечно.
    func test_vc_noisyRoom_endsAtHardCap_despiteConstantActivity() {
        let defaults = UserDefaults(suiteName: "cap-noisy-\(UUID().uuidString)")!
        defaults.set(120, forKey: ConversationMaxDurationSettings.secondsKey)
        let vc = makeVC(defaults: defaults)
        var now: TimeInterval = 1_000
        vc.idleEndClock = { now }
        vc.startConversation()
        vc.conversationState = .listening
        while now + 5 < 1_000 + 120 {
            now += 5
            vc.noteConversationActivity()
            vc.idleEndTick()
            XCTAssertTrue(vc.isSessionActive, "до потолка разговор жив (t=\(now - 1_000))")
        }
        now = 1_000 + 121
        vc.noteConversationActivity()
        vc.idleEndTick()
        XCTAssertFalse(vc.isSessionActive, "потолок длительности: разговор завершён, несмотря на активность")
        XCTAssertEqual(vc.conversationState, .idle)
        XCTAssertNil(vc.idleEndTimer)
    }

    /// Застрявшее «говорю» (воспроизведение не вернуло состояние) тоже не вечно.
    func test_vc_stuckSpeaking_endsAtHardCap() {
        let defaults = UserDefaults(suiteName: "cap-stuck-\(UUID().uuidString)")!
        defaults.set(120, forKey: ConversationMaxDurationSettings.secondsKey)
        let vc = makeVC(defaults: defaults)
        var now: TimeInterval = 50
        vc.idleEndClock = { now }
        vc.startConversation()
        vc.conversationState = .speaking
        now += 121
        vc.idleEndTick()
        XCTAssertTrue(vc.isSessionActive, "посреди ответа AI — даём договорить")
        now += ConversationMaxDurationSettings.speakingGrace
        vc.idleEndTick()
        XCTAssertFalse(vc.isSessionActive, "запас кончился — застрявшее «говорю» не вечно")
    }

    /// Потолок не зависит от тумблера «Завершать после тишины».
    func test_vc_hardCap_worksWhenSilenceEndDisabled() {
        let defaults = UserDefaults(suiteName: "cap-off-\(UUID().uuidString)")!
        defaults.set(false, forKey: ConversationIdleEndSettings.enabledKey)
        defaults.set(120, forKey: ConversationMaxDurationSettings.secondsKey)
        let vc = makeVC(defaults: defaults)
        var now: TimeInterval = 5
        vc.idleEndClock = { now }
        vc.startConversation()
        XCTAssertNil(vc.idleEndPolicy)
        vc.conversationState = .listening
        now += 119
        vc.idleEndTick()
        XCTAssertTrue(vc.isSessionActive)
        now += 2
        vc.idleEndTick()
        XCTAssertFalse(vc.isSessionActive)
    }

    /// Новая сессия получает новый отсчёт, а не остаток прошлой.
    func test_vc_newSession_restartsHardCapClock() {
        let defaults = UserDefaults(suiteName: "cap-restart-\(UUID().uuidString)")!
        defaults.set(120, forKey: ConversationMaxDurationSettings.secondsKey)
        let vc = makeVC(defaults: defaults)
        var now: TimeInterval = 0
        vc.idleEndClock = { now }
        vc.startConversation()
        vc.conversationState = .listening
        now += 121
        vc.idleEndTick()
        XCTAssertFalse(vc.isSessionActive)
        now += 10
        vc.startConversation()
        vc.conversationState = .listening
        now += 60
        vc.noteConversationActivity()
        vc.idleEndTick()
        XCTAssertTrue(vc.isSessionActive, "60 с новой сессии — потолок ещё не наступил")
        vc.stopConversation()
    }
}
