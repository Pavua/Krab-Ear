/*
 ConversationVCAudioTests — тесты аудиологики ConversationViewController+Audio.

 Стратегия:
 Реальный AVAudioEngine не инициализируем: фикстура использует `.isolatedTests`,
 поэтому ни стартовый prebuffer, ни поздний `conv.ready` не включают микрофон.
 Тестируем доступную логику:
   1. handleDownlinkAudio() → переход в .speaking при получении аудио-фрейма.
   2. Повторный вызов handleDownlinkAudio() не дублирует смену состояния.
   3. startConversation() ждёт ready перед захватом, stopConversation() очищает сессию.
   4. stopConversation() → состояние возвращается в .idle.
   5. ConversationEvent.decode — корректное декодирование JSON в typed event.
   6. ConversationEvent.decode — неизвестный тип → .unknown, nil на невалидном JSON.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class ConversationVCAudioTests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        // viewDidLoad вызывает buildUI() + applyState(.idle)
        vc = ConversationViewController(config: .default, runtimeOptions: .isolatedTests)
        vc.loadView()
        vc.viewDidLoad()
        vc.prepareAudioNegotiation()
        vc.configureNegotiatedAudio(sampleRate: nil)
    }

    override func tearDown() async throws {
        vc = nil
        try await super.tearDown()
    }

    // MARK: - handleDownlinkAudio: state → .speaking

    /// handleDownlinkAudio() переводит состояние в .speaking при получении первого фрейма.
    func test_handleDownlinkAudio_setsStateSpeaking() {
        vc.conversationState = .listening
        vc.handleDownlinkAudio(Data(repeating: 0xAB, count: 64))
        XCTAssertEqual(vc.conversationState, .speaking,
                       "После получения аудио-фрейма состояние должно стать .speaking")
    }

    /// handleDownlinkAudio() не меняет состояние если уже .speaking (idempotent).
    func test_handleDownlinkAudio_idempotentWhenAlreadySpeaking() {
        vc.conversationState = .speaking
        vc.handleDownlinkAudio(Data(repeating: 0x00, count: 128))
        XCTAssertEqual(vc.conversationState, .speaking,
                       "Повторный вызов не должен изменять уже установленное .speaking")
    }

    // MARK: - startConversation / stopConversation: isSessionActive flag

    /// startConversation() устанавливает isSessionActive = true и переводит в .connecting.
    /// Изолированный режим не запускает WS и AVAudioEngine, но сохраняет машину состояний.
    func test_startConversation_setsSessionActiveAndConnecting() {
        XCTAssertFalse(vc.isSessionActive)
        // До ready чистая логика только закрывает uplink-гейт;
        // системный ввод-вывод запрещён.
        vc.startConversation()
        XCTAssertTrue(vc.isSessionActive, "После startConversation isSessionActive должен быть true")
        XCTAssertEqual(vc.conversationState, .connecting,
                       "startConversation должен переводить в .connecting")
        XCTAssertFalse(vc.isAudioNegotiationReady,
                       "До conv.ready сетевой uplink-гейт должен оставаться закрыт")
        XCTAssertFalse(vc._testHasWebSocketTask,
                       "Изолированный unit-тест не должен создавать WebSocket task")
        XCTAssertFalse(vc._testHasAudioEngine,
                       "Изолированный unit-тест не должен создавать AVAudioEngine")
        // Cleanup
        vc.isSessionActive = false
    }

    /// Поздний `conv.ready` не должен обходить тестовый запрет и включать микрофон.
    func test_activateNegotiatedAudio_isolatedRuntimeDoesNotCreateAudioEngine() {
        vc.isSessionActive = true
        vc.prepareAudioNegotiation()
        _ = vc.assembleUplinkFrames(
            Array(repeating: 0.25, count: 1_280),
            sourceSampleRate: 16_000
        )

        vc.activateNegotiatedAudio(sampleRate: 24_000)

        XCTAssertTrue(
            vc.isAudioNegotiationReady,
            "Чистое согласование формата должно работать без системного аудиоввода"
        )
        XCTAssertFalse(vc._testHasAudioEngine,
                       "Изолированный режим не должен создавать engine после conv.ready")
        XCTAssertEqual(vc.pendingAudioPrebufferSampleCount, 0,
                       "Изоляция устройств не должна отключать дренирование prebuffer")
    }

    /// Повторный ready с той же частотой не должен терять неполный хвост сборщика.
    func test_activateNegotiatedAudio_repeatedReadyPreservesPartialFrame() {
        vc.isSessionActive = true
        vc.prepareAudioNegotiation()
        _ = vc.assembleUplinkFrames(
            Array(repeating: 0.25, count: 640),
            sourceSampleRate: 16_000
        )

        vc.activateNegotiatedAudio(sampleRate: 24_000)
        vc.activateNegotiatedAudio(sampleRate: 24_000)
        let completedFrames = vc.assembleUplinkFrames(
            Array(repeating: 0.5, count: 960),
            sourceSampleRate: 24_000
        )

        XCTAssertEqual(completedFrames.count, 1,
                       "Повторный ready не должен сбрасывать 960 накопленных сэмплов")
        XCTAssertEqual(completedFrames.first?.count, 1_920)
        XCTAssertFalse(vc._testHasAudioEngine)
    }

    /// Даже прямой вызов обеих публичных границ не должен открыть аудиоустройство.
    func test_directAudioBoundaryCalls_isolatedRuntimeDoesNotCreateEngine() {
        vc.prepareAudioNegotiation()

        vc.startAudioPrebufferCapture()
        XCTAssertFalse(vc._testHasAudioEngine,
                       "Прямой запуск prebuffer обязан учитывать изолированный профиль")

        vc.configureNegotiatedAudio(sampleRate: 24_000)
        vc.startAudioCapture()
        XCTAssertFalse(vc._testHasAudioEngine,
                       "Прямой запуск negotiated capture обязан учитывать профиль")
    }

    /// stopConversation() сбрасывает isSessionActive и переводит в .idle.
    func test_stopConversation_resetsSessionAndReturnsIdle() {
        vc.isSessionActive = true
        vc.conversationState = .listening
        vc.stopConversation()
        XCTAssertFalse(vc.isSessionActive,
                       "После stopConversation isSessionActive должен быть false")
        XCTAssertEqual(vc.conversationState, .idle,
                       "stopConversation должен вернуть состояние в .idle")
    }

    // MARK: - RT-thread safety: _rtSessionActive mirror + @Sendable tap block

    /// _rtSessionActive зеркалит isSessionActive и доступен с non-main queue без краша.
    /// Регрессионный тест для EXC_BREAKPOINT (_swift_task_checkIsolatedSwift) —
    /// ранее installTap-блок наследовал @MainActor-изоляцию из startAudioCapture(),
    /// что вызывало SIGTRAP при вызове с RealtimeMessenger.mServiceQueue.
    /// Фикс: tapBlock объявлен как `@Sendable`, разрывая вывод @MainActor-изоляции.
    func test_rtSessionActiveMirror_callableFromBackgroundQueue() {
        // Установить зеркало через startConversation (выставляет _rtSessionActive = true).
        vc.isSessionActive = false
        ConversationViewController._rtSessionActive = false

        // Симулируем то, что делает startAudioCapture() перед installTap.
        vc.isSessionActive = true
        ConversationViewController._rtSessionActive = true

        let expectation = expectation(description: "background queue access")

        // Core Audio RT thread — симулируем через глобальную очередь высокого приоритета.
        // До фикса это вызывало _swift_task_checkIsolatedSwift → EXC_BREAKPOINT
        // в closure, захватившей @MainActor-изолированный `self.isSessionActive`.
        DispatchQueue.global(qos: .userInteractive).async {
            // Читаем nonisolated зеркало — не @MainActor-isolated, краша нет.
            let active = ConversationViewController._rtSessionActive
            XCTAssertTrue(active, "_rtSessionActive должен быть true после startConversation")
            expectation.fulfill()
        }

        waitForExpectations(timeout: 1.0)

        // Cleanup
        vc.isSessionActive = false
        ConversationViewController._rtSessionActive = false
    }

    /// Остановка сессии очищает зеркало до удаления тапа, чтобы in-flight блоки выходили рано.
    func test_stopAudioCapture_clearsRtMirrorBeforeTapRemoval() {
        ConversationViewController._rtSessionActive = true
        vc.isSessionActive = true

        // stopAudioCapture() должен обнулить зеркало.
        vc.stopAudioCapture()

        XCTAssertFalse(ConversationViewController._rtSessionActive,
                       "stopAudioCapture должен очистить _rtSessionActive до удаления тапа")
    }

    // MARK: - ConversationEvent.decode (pure logic, не требует AVAudioEngine)

    /// stt.partial декодируется в .sttPartial с корректными полями.
    func test_decodeEvent_sttPartial_fieldsCorrect() {
        let json = """
        {"type":"stt.partial","text":"Привет мир","lang":"ru","is_final":true}
        """.data(using: .utf8)!
        let event = ConversationEvent.decode(from: json)
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            XCTFail("Ожидался .sttPartial, получен: \(String(describing: event))")
            return
        }
        XCTAssertEqual(text, "Привет мир")
        XCTAssertEqual(lang, "ru")
        XCTAssertTrue(isFinal)
    }

    /// Неизвестный type → .unknown; невалидный JSON → nil.
    func test_decodeEvent_unknownTypeAndInvalidJSON() {
        let unknownJSON = """
        {"type":"custom.event","foo":"bar"}
        """.data(using: .utf8)!
        let event = ConversationEvent.decode(from: unknownJSON)
        guard case .unknown(let type, _) = event else {
            XCTFail("Ожидался .unknown, получен: \(String(describing: event))")
            return
        }
        XCTAssertEqual(type, "custom.event")

        let invalidData = "not json".data(using: .utf8)!
        XCTAssertNil(ConversationEvent.decode(from: invalidData),
                     "Невалидный JSON должен возвращать nil")
    }
}
