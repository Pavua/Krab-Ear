import AppKit
import XCTest
@testable import KrabEarAgent

/// Живая проверка владельца 25.11: первые слова после «Краб» терялись, разговор не завершался
/// сам и после «Прервать» ассистент «просыпался» от шума. Pre-roll, «Завершить», тишина.
@MainActor
final class WakePrerollAndIdleEndTests: XCTestCase {

    // MARK: - Декодер pre-roll

    private func response(_ pcm: [Int16], rate: Int = 16_000, format: String = "pcm_s16le_mono_b64",
                          available: Bool = true) -> [String: Any] {
        var data = Data()
        for v in pcm { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        return ["ok": true, "result": [
            "ok": true, "available": available, "sample_rate": rate, "format": format,
            "samples": pcm.count, "pcm_b64": data.base64EncodedString(), "age_ms": 120,
        ] as [String: Any]]
    }

    func test_decoder_int16LE_toFloat() {
        let samples = WakePrerollDecoder.decode(response([0, 16384, -32768, 32767]))
        XCTAssertEqual(samples?.count, 4)
        XCTAssertEqual(samples?[0], 0)
        XCTAssertEqual(samples?[1] ?? 0, 0.5, accuracy: 1e-6)
        XCTAssertEqual(samples?[2] ?? 0, -1, accuracy: 1e-6)
        XCTAssertEqual(samples?[3] ?? 0, 32767.0 / 32768.0, accuracy: 1e-6)
    }

    func test_decoder_rejectsUnavailableForeignFormatAndGarbage() {
        XCTAssertNil(WakePrerollDecoder.decode(response([1, 2], available: false)))
        XCTAssertNil(WakePrerollDecoder.decode(response([1, 2], rate: 24_000)))
        XCTAssertNil(WakePrerollDecoder.decode(response([1, 2], format: "f32")))
        XCTAssertNil(WakePrerollDecoder.decode(nil), "старый backend без метода — без pre-roll")
        XCTAssertNil(WakePrerollDecoder.decode(["ok": false]))
        XCTAssertNil(WakePrerollDecoder.decode(["result": ["ok": true, "available": true, "sample_rate": 16_000,
                                                           "format": "pcm_s16le_mono_b64", "pcm_b64": "!!!"]]))
        let tooLong = [Int16](repeating: 1, count: WakePrerollDecoder.maxSamples + 1)
        XCTAssertNil(WakePrerollDecoder.decode(response(tooLong)))
    }

    // MARK: - Ящик pre-roll

    func test_mailbox_wakeConversation_getsPreroll_once() {
        let box = WakePrerollMailbox()
        box.arm(at: 100)
        box.deliver([0.1, 0.2], at: 100.05)
        XCTAssertTrue(box.claimIfArmed(at: 100.2))
        XCTAssertEqual(box.take(at: 101), [0.1, 0.2])
        XCTAssertNil(box.take(at: 101), "одноразовый")
    }

    func test_mailbox_prerollArrivingAfterClaim_isStillUsedBeforeReady() {
        let box = WakePrerollMailbox()
        box.arm(at: 10)
        XCTAssertTrue(box.claimIfArmed(at: 10.1))
        box.deliver([0.3], at: 10.4)
        XCTAssertEqual(box.take(at: 11.5), [0.3])
    }

    func test_mailbox_hotkeyConversation_clearsStalePreroll() {
        let box = WakePrerollMailbox()
        box.arm(at: 10)
        box.deliver([0.3], at: 10.1)
        // Детекция не запустила разговор (размыкатель/уже идёт), а через 5 с — горячая клавиша.
        XCTAssertFalse(box.claimIfArmed(at: 15))
        XCTAssertNil(box.take(at: 15.5), "чужой звук не попадает в разговор по горячей клавише")
    }

    func test_mailbox_unclaimedOrExpired_givesNothing() {
        let box = WakePrerollMailbox()
        XCTAssertFalse(box.claimIfArmed(at: 1), "без детекции")
        box.arm(at: 0)
        box.deliver([0.5], at: WakePrerollMailbox.ttl + 1)
        XCTAssertTrue(box.claimIfArmed(at: 0.5))
        XCTAssertNil(box.take(at: 1), "доставка позже TTL не принимается")
    }

    func test_prebuffer_prepend_putsPrerollFirst_andKeepsLimit() {
        var buffer = ConversationAudioPrebuffer(maxSampleCount: 5)
        buffer.append([3, 4, 5])
        buffer.prepend([1, 2])
        XCTAssertEqual(buffer.drain(), [1, 2, 3, 4, 5])
        buffer.append([7, 8, 9, 10])
        buffer.prepend([5, 6])
        XCTAssertEqual(buffer.droppedSampleCount, 1)
        XCTAssertEqual(buffer.drain(), [5, 6, 7, 8, 9], "переполнение срезает хвост, не pre-roll")
    }

    // MARK: - Политика тишины

    func test_idlePolicy_endsAfterTimeout_onlyWhileListening() {
        var policy = ConversationIdleEndPolicy(timeout: 30, startedAt: 0)
        XCTAssertFalse(policy.shouldEnd(now: 29, state: .listening))
        XCTAssertTrue(policy.shouldEnd(now: 30, state: .listening))
        for state in [ConversationState.speaking, .thinking, .connecting] {
            var p = ConversationIdleEndPolicy(timeout: 30, startedAt: 0)
            XCTAssertFalse(p.shouldEnd(now: 100, state: state), "\(state): AI говорит/думает или движок грузится")
        }
    }

    func test_idlePolicy_activityAndAiTurnRestartCount() {
        var policy = ConversationIdleEndPolicy(timeout: 30, startedAt: 0)
        policy.noteActivity(at: 20)  // пользователь заговорил
        XCTAssertFalse(policy.shouldEnd(now: 45, state: .listening))
        // AI отвечал до 80 с — отсчёт удерживается и после ответа начинается заново.
        XCTAssertFalse(policy.shouldEnd(now: 80, state: .speaking))
        XCTAssertFalse(policy.shouldEnd(now: 105, state: .listening))
        XCTAssertTrue(policy.shouldEnd(now: 110, state: .listening))
    }

    /// Гейт Opus #2270: VG не шлёт VAD/частичных расшифровок — речь видна только локально.
    /// Пауза на подумать 20 с + монолог: голос каждые 5 с в «слушаю» не даёт завершить на 40 с.
    func test_idlePolicy_localVoiceEvery5s_keepsConversation_silenceEnds() {
        var policy = ConversationIdleEndPolicy(timeout: 30, startedAt: 0)
        var gate = LocalVoiceActivityGate()
        let voice = [Float](repeating: 0.1, count: 1280)
        for t in stride(from: 20.0, through: 40.0, by: 5.0) {
            if gate.isVoice(voice, now: t) { policy.noteActivity(at: t) }
            XCTAssertFalse(policy.shouldEnd(now: t, state: .listening), "говорит — не завершать (\(t) с)")
        }
        XCTAssertFalse(policy.shouldEnd(now: 40, state: .listening))
        XCTAssertFalse(policy.shouldEnd(now: 69, state: .listening))
        XCTAssertTrue(policy.shouldEnd(now: 70, state: .listening), "30 с тишины после последней речи")
    }

    func test_localVoiceGate_thresholdAndThrottle() {
        var gate = LocalVoiceActivityGate()
        let quiet = [Float](repeating: 0.005, count: 1280)  // шум комнаты
        let loud = [Float](repeating: 0.05, count: 1280)
        XCTAssertFalse(gate.isVoice(quiet, now: 0))
        XCTAssertTrue(gate.isVoice(loud, now: 0))
        XCTAssertFalse(gate.isVoice(loud, now: 0.3), "не чаще раза в 0,5 с")
        XCTAssertTrue(gate.isVoice(loud, now: 0.5))
        XCTAssertFalse(gate.isVoice([], now: 10))
    }

    func test_idleSettings_defaultsAndBounds() {
        let defaults = UserDefaults(suiteName: "idle-\(UUID().uuidString)")!
        XCTAssertTrue(ConversationIdleEndSettings.isEnabled(in: defaults), "по умолчанию включено")
        XCTAssertEqual(ConversationIdleEndSettings.seconds(in: defaults), 30)
        defaults.set(false, forKey: ConversationIdleEndSettings.enabledKey)
        XCTAssertFalse(ConversationIdleEndSettings.isEnabled(in: defaults))
        defaults.set(3, forKey: ConversationIdleEndSettings.secondsKey)
        XCTAssertEqual(ConversationIdleEndSettings.seconds(in: defaults), 10)
        defaults.set(9_999, forKey: ConversationIdleEndSettings.secondsKey)
        XCTAssertEqual(ConversationIdleEndSettings.seconds(in: defaults), 300)
        XCTAssertTrue(ConversationIdleEndSettings.choices.allSatisfy { ConversationIdleEndSettings.range.contains($0) })
        XCTAssertEqual(ConversationIdleEndSettings.choiceTitle(30), "30 с")
        XCTAssertEqual(ConversationIdleEndSettings.choiceTitle(120), "2 мин")
    }

    // MARK: - Контроллер разговора (изолированный режим)

    private func makeVC(defaults: UserDefaults) -> ConversationViewController {
        let config = ConversationConfig(wsURLString: "ws://127.0.0.1:19999/v1/conversation", apiKey: "",
                                        languageHint: "auto", engine: "auto", brain: "auto")
        let vc = ConversationViewController(config: config, runtimeOptions: .isolatedTests, userDefaults: defaults)
        vc.loadView()
        vc.viewDidLoad()
        return vc
    }

    func test_vc_silenceInListening_endsSession() {
        let defaults = UserDefaults(suiteName: "idle-vc-\(UUID().uuidString)")!
        defaults.set(10, forKey: ConversationIdleEndSettings.secondsKey)
        let vc = makeVC(defaults: defaults)
        var now: TimeInterval = 1_000
        vc.idleEndClock = { now }
        vc.startConversation()
        XCTAssertNotNil(vc.idleEndPolicy)
        vc.conversationState = .listening
        now += 9
        vc.idleEndTick()
        XCTAssertTrue(vc.isSessionActive)
        vc.handleDownlinkEvent(.sttPartial(text: "привет", lang: "ru", isFinal: false))
        now += 9
        vc.idleEndTick()
        XCTAssertTrue(vc.isSessionActive, "речь пользователя сбросила отсчёт")
        now += 2
        vc.idleEndTick()
        XCTAssertFalse(vc.isSessionActive, "10 с тишины в «слушаю» — разговор завершён")
        // Завершение идёт штатным stopConversation(): .end, закрытие WS и микрофона, оверлей скрыт.
        XCTAssertEqual(vc.conversationState, .idle)
        XCTAssertNil(vc.idleEndTimer)
    }

    func test_vc_micVoice_resetsSilence_andStopResetsPrerollMailbox() {
        let defaults = UserDefaults(suiteName: "idle-mic-\(UUID().uuidString)")!
        defaults.set(10, forKey: ConversationIdleEndSettings.secondsKey)
        let vc = makeVC(defaults: defaults)
        var now: TimeInterval = 500
        vc.idleEndClock = { now }
        vc.startConversation()
        vc.conversationState = .listening
        let voice = [Float](repeating: 0.1, count: 1280)
        for _ in 0..<5 {  // 15 с монолога: голос каждые 3 с
            now += 3
            vc.processAudioSamples(voice, sourceSampleRate: 16_000)
            vc.idleEndTick()
        }
        XCTAssertTrue(vc.isSessionActive, "речь в микрофон держит разговор без событий VG")
        WakePrerollMailbox.shared.arm(at: now)
        WakePrerollMailbox.shared.deliver([0.2], at: now)
        now += 10
        vc.idleEndTick()
        XCTAssertFalse(vc.isSessionActive, "10 с тишины — завершён")
        XCTAssertFalse(WakePrerollMailbox.shared.claimIfArmed(at: now), "стоп сбросил недозабранный pre-roll")
    }

    /// Ревью Codex #2270: после обычного ответа воспроизведение назад в «слушаю» не переключало.
    func test_playbackSettle_onlyAfterQueueAndQuietPeriod() {
        let t0 = Date(timeIntervalSince1970: 1_000)
        func settled(_ state: ConversationState, queueEnds: Double, lastAI: Double, now: Double) -> Bool {
            ConversationPlaybackSettle.isSettled(
                state: state, playbackQueueEndsAt: t0.addingTimeInterval(queueEnds), echoTail: 0.35,
                lastAISpeechAt: t0.addingTimeInterval(lastAI), now: t0.addingTimeInterval(now))
        }
        XCTAssertFalse(settled(.speaking, queueEnds: 10, lastAI: 5, now: 9), "очередь ещё играет")
        XCTAssertFalse(settled(.speaking, queueEnds: 1, lastAI: 5, now: 6), "текст пришёл, звук может догнать")
        XCTAssertTrue(settled(.speaking, queueEnds: 1, lastAI: 5, now: 7.6))
        XCTAssertFalse(settled(.thinking, queueEnds: 0, lastAI: 0, now: 100), "только из «говорю»")
    }

    func test_vc_afterAiReplyAndSilence_returnsToListening_thenAutoEnds() {
        let defaults = UserDefaults(suiteName: "idle-reply-\(UUID().uuidString)")!
        defaults.set(10, forKey: ConversationIdleEndSettings.secondsKey)
        let vc = makeVC(defaults: defaults)
        var uptime: TimeInterval = 100
        var wall = Date(timeIntervalSince1970: 50_000)
        vc.idleEndClock = { uptime }
        vc.playbackClock = { wall }
        vc.startConversation()
        vc.conversationState = .listening
        vc.handleDownlinkEvent(.replyFinal(text: "Готово."))
        XCTAssertEqual(vc.conversationState, .speaking)
        func advance(_ seconds: TimeInterval) { uptime += seconds; wall = wall.addingTimeInterval(seconds); vc.idleEndTick() }
        advance(1)
        XCTAssertEqual(vc.conversationState, .speaking, "сразу после текста не переключаемся")
        advance(2)
        XCTAssertEqual(vc.conversationState, .listening, "ответ доигран — снова «слушаю»")
        advance(9)
        XCTAssertTrue(vc.isSessionActive)
        advance(2)
        XCTAssertFalse(vc.isSessionActive, "10 с тишины после ответа — разговор завершён")
    }

    /// Нит гейта #2270: с выключенной настройкой разговор сам не завершается, но метка после
    /// ответа всё равно возвращается в «слушаю» (тик идёт всю сессию).
    func test_vc_idleEndDisabled_noSilenceEnd_butLabelSettles() {
        let defaults = UserDefaults(suiteName: "idle-off-\(UUID().uuidString)")!
        defaults.set(false, forKey: ConversationIdleEndSettings.enabledKey)
        let vc = makeVC(defaults: defaults)
        var uptime: TimeInterval = 10
        var wall = Date(timeIntervalSince1970: 70_000)
        vc.idleEndClock = { uptime }
        vc.playbackClock = { wall }
        vc.startConversation()
        XCTAssertNil(vc.idleEndPolicy)
        XCTAssertNotNil(vc.idleEndTimer)
        vc.conversationState = .listening
        vc.handleDownlinkEvent(.replyFinal(text: "Готово."))
        uptime += 3; wall = wall.addingTimeInterval(3); vc.idleEndTick()
        XCTAssertEqual(vc.conversationState, .listening, "метка не висит на «говорю»")
        // 500 с — дольше любого порога тишины, но короче жёсткого потолка сессии (600 с, ConversationHardCapTests).
        uptime += 500; wall = wall.addingTimeInterval(500); vc.idleEndTick()
        XCTAssertTrue(vc.isSessionActive, "автозавершение выключено")
        vc.stopConversation()
        XCTAssertNil(vc.idleEndTimer)
    }

    func test_overlay_endButton_alwaysVisible_andEndsSession() {
        let defaults = UserDefaults(suiteName: "end-\(UUID().uuidString)")!
        let vc = makeVC(defaults: defaults)
        vc.startConversation()
        guard let overlay = vc.statusOverlay else { return XCTFail("нет оверлея") }
        for state in [ConversationState.listening, .thinking, .speaking] {
            overlay.update(state: state)
            XCTAssertTrue(overlay._testEndButtonVisible, "«Завершить» видна в \(state)")
        }
        overlay._testTapEnd()
        XCTAssertFalse(vc.isSessionActive)
    }
}
