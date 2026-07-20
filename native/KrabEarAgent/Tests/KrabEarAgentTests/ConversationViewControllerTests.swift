/*
 ConversationViewControllerTests — XCTest suite для ConversationViewController (Wave 197).

 Покрывает все 11 сценариев Voice Assistant Phase 1:
   1.  test_initial_state_idle              — начальное состояние idle при инициализации
   2.  test_start_session_via_button        — onStartStopTapped() запускает сессию
   3.  test_start_session_via_hotkey_double_tap — startConversation() как входная точка для hotkey
   4.  test_stop_session                    — stopConversation() возвращает в idle
   5.  test_streaming_message_display       — sttPartial !isFinal показывается без сохранения
   6.  test_user_message_appended           — sttPartial isFinal=true добавляет «Вы: …» в буфер
   7.  test_assistant_message_appended      — replyFinal добавляет строку «AI: …» + .speaking
   8.  test_handles_session_error_gracefully — error event переводит в .error и останавливает
   9.  test_unicode_messages                — кирилица/emoji в транскрипте не ломает буфер
  10.  test_concurrent_session_blocked      — повторный startConversation игнорируется
  11.  test_session_persist_across_view_switch — transcriptBuffer сохраняется без viewDidLoad

 Стратегия: нет реального WS/микрофона — тестируем мутации state/buffer/UI-флагов.
 @MainActor required — ConversationViewController изолирован на главном акторе.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - ConversationViewControllerTests

@MainActor
final class ConversationViewControllerTests: XCTestCase {

    // MARK: - Fixtures

    private var vc: ConversationViewController!

    /// Создаёт VC в изолированном режиме: URL остаётся реалистичным,
    /// но сокет не открывается.
    private func makeVC(wsURL: String = "ws://127.0.0.1:19999/v1/conversation") -> ConversationViewController {
        let config = ConversationConfig(
            wsURLString: wsURL,
            apiKey:       "",
            languageHint: "auto",
            engine:       "auto",
            brain:        "auto"
        )
        let vc = ConversationViewController(config: config, runtimeOptions: .isolatedTests)
        vc.loadView()
        vc.viewDidLoad()
        return vc
    }

    override func setUp() async throws {
        try await super.setUp()
        vc = makeVC()
        ConversationViewController._resetTestState()
    }

    override func tearDown() async throws {
        // Если сессия активна — гасим принудительно чтобы не течь WS/audio.
        if vc.isSessionActive {
            vc.isSessionActive = false
        }
        ConversationViewController._rtSessionActive = false
        vc = nil
        try await super.tearDown()
    }

    // MARK: - 1. test_initial_state_idle

    /// Контроллер должен стартовать в состоянии .idle.
    /// isSessionActive = false, transcriptBuffer пустой.
    func test_initial_state_idle() {
        XCTAssertEqual(vc.conversationState, .idle,
                       "Начальное состояние должно быть .idle")
        XCTAssertFalse(vc.isSessionActive,
                       "Сессия не должна быть активна при инициализации")
        XCTAssertEqual(vc.transcriptBuffer, "",
                       "transcriptBuffer должен быть пустым при инициализации")
        XCTAssertEqual(vc.statusLabel.stringValue, "⚪ Готов",
                       "statusLabel должен показывать «⚪ Готов»")
        XCTAssertTrue(vc.startButton.isEnabled,
                      "startButton должна быть доступна в .idle")
        XCTAssertTrue(vc.interruptButton.isHidden,
                      "interruptButton должна быть скрыта в .idle")
    }

    /// Продовый и тестовый профили явно фиксируют границу системного ввода-вывода.
    /// Этот тест проверяет контракт профилей, не запуская сокет или микрофон.
    func test_runtimeOptions_profilesHaveExpectedSystemIOPolicy() {
        XCTAssertTrue(ConversationRuntimeOptions.production.opensWebSocket)
        XCTAssertTrue(ConversationRuntimeOptions.production.capturesAudio)
        XCTAssertFalse(ConversationRuntimeOptions.isolatedTests.opensWebSocket)
        XCTAssertFalse(ConversationRuntimeOptions.isolatedTests.capturesAudio)

        let defaultController = ConversationViewController(config: .default)
        XCTAssertTrue(defaultController.runtimeOptions.opensWebSocket,
                      "Инициализатор без профиля должен сохранять продовый WebSocket")
        XCTAssertTrue(defaultController.runtimeOptions.capturesAudio,
                      "Инициализатор без профиля должен сохранять продовый захват аудио")
    }

    // MARK: - 2. test_start_session_via_button

    /// Нажатие кнопки «Начать разговор» (onStartStopTapped) при неактивной сессии
    /// должно вызвать startConversation() → isSessionActive=true, state=.connecting.
    func test_start_session_via_button() {
        XCTAssertFalse(vc.isSessionActive, "Precondition: сессия не активна")

        vc.onStartStopTapped()

        XCTAssertTrue(vc.isSessionActive,
                      "После нажатия кнопки isSessionActive должен стать true")
        XCTAssertEqual(vc.conversationState, .connecting,
                       "После нажатия кнопки state должен стать .connecting")
        XCTAssertFalse(vc.startButton.isEnabled,
                       "startButton должна быть disabled пока идёт разговор")
        XCTAssertEqual(vc.startButton.title, "Идёт разговор...",
                       "startButton.title должна измениться на «Идёт разговор...»")
    }

    // MARK: - 3. test_start_session_via_hotkey_double_tap

    /// startConversation() — публичная входная точка для двойного тапа hotkey (Wave 194 / PR 1.5).
    /// Поведение идентично нажатию кнопки: state → .connecting, isSessionActive = true.
    func test_start_session_via_hotkey_double_tap() {
        // HotkeyDoubleTapDetector вызывает startConversation() напрямую.
        XCTAssertEqual(vc.conversationState, .idle,
                       "Precondition: начальное состояние .idle")
        XCTAssertFalse(vc.isSessionActive)

        vc.startConversation()

        XCTAssertTrue(vc.isSessionActive,
                      "Hotkey double-tap должен устанавливать isSessionActive=true")
        XCTAssertEqual(vc.conversationState, .connecting,
                       "Hotkey double-tap должен переводить в .connecting")
        // Проверяем что transcriptBuffer обнуляется при старте нового сеанса.
        XCTAssertEqual(vc.transcriptBuffer, "",
                       "transcriptBuffer должен сбрасываться при старте нового сеанса")
    }

    // MARK: - 4. test_stop_session

    /// stopConversation() должен сбросить isSessionActive и вернуть state в .idle.
    func test_stop_session() {
        // Устанавливаем активный сеанс вручную.
        vc.isSessionActive = true
        vc.conversationState = .listening

        vc.stopConversation()

        XCTAssertFalse(vc.isSessionActive,
                       "isSessionActive должен быть false после stopConversation")
        XCTAssertEqual(vc.conversationState, .idle,
                       "state должен вернуться в .idle после stopConversation")
        XCTAssertTrue(vc.startButton.isEnabled,
                      "startButton должна быть доступна после остановки")
        XCTAssertTrue(vc.interruptButton.isHidden,
                      "interruptButton должна быть скрыта после остановки")
    }

    /// Поздние данные старого WebSocket и старого аудиоперехвата не должны попадать
    /// в быстро запущенный новый разговор.
    func test_staleSessionGeneration_rejectsWebSocketAndAudioCallbacks() {
        vc.isSessionActive = true
        vc.prepareAudioNegotiation()
        let staleGeneration = vc.beginConversationGeneration()
        let currentGeneration = vc.beginConversationGeneration()
        let reply = #"{"type":"conv.reply_final","data":{"text":"Старый ответ"}}"#

        vc.handleWSMessage(.string(reply), generation: staleGeneration)
        vc.processAudioSamples(
            Array(repeating: 0.25, count: 320),
            sourceSampleRate: 16_000,
            generation: staleGeneration
        )

        XCTAssertTrue(vc.transcriptBuffer.isEmpty)
        XCTAssertEqual(vc.pendingAudioPrebufferSampleCount, 0)

        vc.handleWSMessage(.string(reply), generation: currentGeneration)
        vc.processAudioSamples(
            Array(repeating: 0.25, count: 320),
            sourceSampleRate: 16_000,
            generation: currentGeneration
        )

        XCTAssertTrue(vc.transcriptBuffer.contains("Старый ответ"))
        XCTAssertEqual(vc.pendingAudioPrebufferSampleCount, 320)
    }

    /// Даже актуальный UUID отвергается после остановки сессии.
    func test_sessionGeneration_requiresActiveSession() {
        vc.isSessionActive = true
        let generation = vc.beginConversationGeneration()
        XCTAssertTrue(vc.acceptsConversationCallback(generation))

        vc.isSessionActive = false
        XCTAssertFalse(vc.acceptsConversationCallback(generation))
    }

    // MARK: - 5. test_streaming_message_display

    /// sttPartial с isFinal=false должен показывать частичный текст
    /// без добавления в transcriptBuffer (интерактивный preview).
    func test_streaming_message_display() {
        vc.transcriptBuffer = ""
        vc.isSessionActive = true
        vc.conversationState = .listening

        let partialText = "Привет"
        vc.handleDownlinkEvent(.sttPartial(text: partialText, lang: "ru", isFinal: false))

        // transcriptBuffer НЕ должен содержать частичный текст.
        XCTAssertEqual(vc.transcriptBuffer, "",
                       "isFinal=false НЕ должен добавлять строку в transcriptBuffer")
        // transcriptView.string ДОЛЖЕН содержать partial preview.
        XCTAssertTrue(vc.transcriptView.string.contains(partialText),
                      "transcriptView.string должен содержать partial текст как preview")
        XCTAssertEqual(vc.conversationState, .listening,
                       "Partial event не должен менять state из .listening")
    }

    // MARK: - 6. test_user_message_appended

    /// sttPartial с isFinal=true должен добавить строку «Вы: <текст>» в transcriptBuffer.
    func test_user_message_appended() {
        vc.transcriptBuffer = ""
        vc.isSessionActive = true
        vc.conversationState = .listening

        let finalText = "Как дела?"
        vc.handleDownlinkEvent(.sttPartial(text: finalText, lang: "ru", isFinal: true))

        XCTAssertTrue(vc.transcriptBuffer.contains("Вы:"),
                      "Финальный STT должен добавлять строку с префиксом «Вы:»")
        XCTAssertTrue(vc.transcriptBuffer.contains(finalText),
                      "Финальный STT должен содержать оригинальный текст")
        XCTAssertEqual(vc.transcriptView.string, vc.transcriptBuffer,
                       "transcriptView.string должен совпадать с transcriptBuffer")
    }

    // MARK: - 6b. test_final_transcript_sets_thinking_state

    /// sttPartial с isFinal=true должен перевести state в .thinking (не только добавить текст) —
    /// это и есть визуальный сигнал «Думаю…» на время ожидания ответа мозга (Волна 3b §5).
    func test_final_transcript_sets_thinking_state() {
        vc.transcriptBuffer = ""
        vc.isSessionActive = true
        vc.conversationState = .listening

        vc.handleDownlinkEvent(.sttPartial(text: "Который час?", lang: "ru", isFinal: true))

        XCTAssertEqual(vc.conversationState, .thinking,
                       "Финальный STT должен переводить state в .thinking (статус «Думает» на время ответа мозга)")
    }

    // MARK: - 7. test_assistant_message_appended

    /// replyFinal → строка «AI: <text>» добавляется в буфер + state → .speaking.
    func test_assistant_message_appended() {
        vc.transcriptBuffer = ""
        vc.isSessionActive = true
        vc.conversationState = .thinking

        // Финальный ответ AI.
        let replyText = "В Москве сейчас солнечно."
        vc.handleDownlinkEvent(.replyFinal(text: replyText))

        XCTAssertTrue(vc.transcriptBuffer.contains("AI:"),
                      "replyFinal должен добавить строку с префиксом «AI:»")
        XCTAssertTrue(vc.transcriptBuffer.contains(replyText),
                      "replyFinal должен содержать оригинальный текст ответа")
        XCTAssertEqual(vc.conversationState, .speaking,
                       "replyFinal должен перевести state в .speaking")
    }

    // MARK: - 8. test_handles_session_error_gracefully

    /// Получение error-события должно:
    ///   - добавить строку с кодом ошибки в transcriptBuffer
    ///   - сбросить isSessionActive (через stopConversation)
    ///   - финальный state = .idle (handleDownlinkEvent: .error → state = .error(msg) → stopConversation → .idle)
    ///   - НЕ бросить исключение
    func test_handles_session_error_gracefully() {
        vc.transcriptBuffer = ""
        vc.isSessionActive = true
        vc.conversationState = .listening

        let errorMsg = "Gateway timeout"
        vc.handleDownlinkEvent(.error(code: "E_TIMEOUT", message: errorMsg))

        // handleDownlinkEvent: sets .error(msg) then calls stopConversation() which sets .idle.
        // Финальный state = .idle (нормальное завершение сессии).
        XCTAssertEqual(vc.conversationState, .idle,
                       "После error event + stopConversation state должен быть .idle")

        // Буфер должен содержать код ошибки и само сообщение.
        XCTAssertTrue(vc.transcriptBuffer.contains("E_TIMEOUT"),
                      "Буфер должен содержать код ошибки")
        XCTAssertTrue(vc.transcriptBuffer.contains(errorMsg),
                      "Буфер должен содержать текст ошибки")

        // Сессия должна быть остановлена.
        XCTAssertFalse(vc.isSessionActive,
                       "isSessionActive должен быть false после error event")
    }

    // MARK: - 9. test_unicode_messages

    /// Многоязычные строки (кирилица, emoji, японский) должны корректно накапливаться в буфере.
    func test_unicode_messages() {
        vc.transcriptBuffer = ""

        let lines: [String] = [
            "Привет, мир! 🌍",
            "Hola, mundo 🎤",
            "こんにちは世界",
            "Спасибо за 🤖 ответ",
        ]

        for line in lines {
            vc.appendTranscriptLine(line)
        }

        // Все строки должны присутствовать в буфере.
        for line in lines {
            XCTAssertTrue(vc.transcriptBuffer.contains(line),
                          "Буфер должен содержать строку: \(line)")
        }

        // Строки объединяются через \n.
        let segments = vc.transcriptBuffer.components(separatedBy: "\n")
        XCTAssertEqual(segments.count, lines.count,
                       "Должно быть \(lines.count) строк разделённых через \\n")
    }

    // MARK: - 10. test_concurrent_session_blocked

    /// Повторный вызов startConversation() при уже активной сессии — no-op.
    /// state и isSessionActive не изменяются (guard !isSessionActive).
    func test_concurrent_session_blocked() {
        // Запустить первую сессию.
        vc.isSessionActive = true
        vc.conversationState = .listening

        // Имитируем второй вызов (например, от двойного клика или второго hotkey).
        vc.startConversation()

        // state должен остаться прежним.
        XCTAssertEqual(vc.conversationState, .listening,
                       "Повторный startConversation не должен менять state")
        XCTAssertTrue(vc.isSessionActive,
                      "isSessionActive должен оставаться true")
        // transcriptBuffer не должен сброситься.
    }

    // MARK: - 11. test_session_persist_across_view_switch

    /// transcriptBuffer и conversationState должны сохраняться при переключении вкладок.
    /// viewDidLoad НЕ вызывается повторно при смене вкладки — состояние персистентно в VC.
    func test_session_persist_across_view_switch() {
        // Накопить данные сессии.
        vc.transcriptBuffer = ""
        vc.appendTranscriptLine("Вы: Привет")
        vc.appendTranscriptLine("AI: Здравствуйте!")
        vc.conversationState = .listening
        vc.isSessionActive = true

        let savedBuffer = vc.transcriptBuffer
        let savedState  = vc.conversationState

        // Имитируем переключение вкладки: viewWillDisappear / viewWillAppear.
        // На macOS NSViewController реализует эти методы, но не сбрасывает state.
        vc.viewWillDisappear()
        vc.viewWillAppear()

        XCTAssertEqual(vc.transcriptBuffer, savedBuffer,
                       "transcriptBuffer должен сохраняться при смене вкладки")
        XCTAssertEqual(vc.conversationState, savedState,
                       "conversationState должен сохраняться при смене вкладки")
        XCTAssertTrue(vc.isSessionActive,
                      "isSessionActive должен сохраняться при смене вкладки")
    }
}
