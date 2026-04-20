/*
 ConversationVCUITests — тесты UI-логики ConversationViewController+UI.

 Стратегия:
 NSView-иерархия не рендерится (headless), но NSTextField.stringValue,
 NSButton.title / isEnabled / isHidden доступны без оконного сервера.
 Тестируем:
   1. ConversationState.localizedLabel — строки для каждого состояния.
   2. applyState(.idle / .connecting / .listening / .thinking / .speaking) —
      startButton.title, isEnabled, interruptButton.isHidden.
   3. statusLabel обновляется при смене conversationState.
   4. appendTranscriptLine() накапливает строки в transcriptBuffer через \n.
   5. updateTranscript() перезаписывает transcriptView.string.
   6. handleDownlinkEvent(.engineLoaded) → строка добавляется в буфер + state .listening.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class ConversationVCUITests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
    }

    override func tearDown() async throws {
        vc = nil
        try await super.tearDown()
    }

    // MARK: - ConversationState.localizedLabel

    /// Каждое состояние имеет ожидаемый локализованный ярлык.
    func test_localizedLabel_allStates() {
        XCTAssertEqual(ConversationState.idle.localizedLabel, "⚪ Готов")
        XCTAssertEqual(ConversationState.connecting.localizedLabel, "🟡 Подключение...")
        XCTAssertEqual(ConversationState.listening.localizedLabel, "🟢 Слушает")
        XCTAssertEqual(ConversationState.thinking.localizedLabel, "🟡 Думает")
        XCTAssertEqual(ConversationState.speaking.localizedLabel, "🔴 Говорит")
        XCTAssertEqual(ConversationState.error("нет сети").localizedLabel, "🔴 Ошибка: нет сети")
    }

    // MARK: - applyState → button/label updates

    /// В состоянии .idle: кнопка «Начать разговор» enabled, interruptButton скрыт.
    func test_applyState_idle_buttonEnabledAndInterruptHidden() {
        vc.conversationState = .idle
        XCTAssertTrue(vc.startButton.isEnabled,
                      "В .idle startButton должна быть доступна")
        XCTAssertEqual(vc.startButton.title, "🎙 Начать разговор",
                       "В .idle startButton должна показывать текст «🎙 Начать разговор»")
        XCTAssertTrue(vc.interruptButton.isHidden,
                      "В .idle interruptButton должна быть скрыта")
    }

    /// В состоянии .speaking: startButton disabled, interruptButton видима.
    func test_applyState_speaking_interruptButtonVisible() {
        vc.conversationState = .speaking
        XCTAssertFalse(vc.startButton.isEnabled,
                       "В .speaking startButton должна быть недоступна")
        XCTAssertFalse(vc.interruptButton.isHidden,
                       "В .speaking interruptButton должна быть видима")
    }

    /// В состоянии .listening: startButton disabled, interruptButton скрыт.
    func test_applyState_listening_interruptHidden() {
        vc.conversationState = .listening
        XCTAssertFalse(vc.startButton.isEnabled,
                       "В .listening startButton должна быть недоступна")
        XCTAssertTrue(vc.interruptButton.isHidden,
                      "В .listening interruptButton должна быть скрыта")
    }

    // MARK: - statusLabel follows conversationState

    /// statusLabel.stringValue обновляется при каждой смене состояния.
    func test_statusLabel_updatesOnStateChange() {
        vc.conversationState = .listening
        XCTAssertEqual(vc.statusLabel.stringValue, "🟢 Слушает")
        vc.conversationState = .thinking
        XCTAssertEqual(vc.statusLabel.stringValue, "🟡 Думает")
        vc.conversationState = .idle
        XCTAssertEqual(vc.statusLabel.stringValue, "⚪ Готов")
    }

    // MARK: - Transcript helpers

    /// appendTranscriptLine() объединяет строки через \n в transcriptBuffer.
    func test_appendTranscriptLine_buildsBuffer() {
        vc.updateTranscript("") // reset
        vc.appendTranscriptLine("Первая строка")
        vc.appendTranscriptLine("Вторая строка")
        XCTAssertEqual(vc.transcriptBuffer, "Первая строка\nВторая строка",
                       "Строки должны объединяться через \\n")
    }

    /// updateTranscript() перезаписывает transcriptView.string.
    func test_updateTranscript_replacesText() {
        vc.updateTranscript("Новый текст")
        XCTAssertEqual(vc.transcriptView.string, "Новый текст",
                       "transcriptView.string должна отражать переданный текст")
        vc.updateTranscript("Заменено")
        XCTAssertEqual(vc.transcriptView.string, "Заменено",
                       "Повторный вызов должен заменить предыдущий текст")
    }

    // MARK: - handleDownlinkEvent

    /// engineLoaded → строка добавляется в буфер, состояние → .listening.
    func test_handleDownlinkEvent_engineLoaded_addsLineAndSetsListening() {
        vc.conversationState = .connecting
        vc.transcriptBuffer  = ""
        vc.handleDownlinkEvent(.engineLoaded(name: "moshi", elapsedSec: 1.23))
        XCTAssertTrue(vc.transcriptBuffer.contains("moshi"),
                      "transcriptBuffer должен содержать имя движка")
        XCTAssertEqual(vc.conversationState, .listening,
                       "После engineLoaded состояние должно быть .listening")
    }
}
