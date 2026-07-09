/*
 ConversationViewController — корневой NSViewController вкладки «Разговор с AI».

 Архитектура (Phase 1.3):
 - ConversationViewController.swift   — жизненный цикл, состояние, точки входа для PR 1.5
 - ConversationViewController+UI.swift       — построение интерфейса
 - ConversationViewController+WebSocket.swift — URLSessionWebSocketTask client
 - ConversationViewController+Audio.swift    — AVAudioEngine capture / playback stubs

 Связи:
 - HistoryPanelController+VoiceTab.swift создаёт и встраивает этот VC в 4-й таб.
 - IPCClient не нужен напрямую — конфиг берётся из ConversationConfig.
*/

import AppKit
import AVFoundation

// Разговор занимает микрофон: агент ставит wake word на паузу на .started
// и возобновляет на .stopped (setupWakeWordConversationObservers в main.swift).
// Post из start/stopConversation — единственной воронки всех путей
// старта/останова (hotkey, кнопка, WS-close, ошибки).
extension Notification.Name {
    static let krabConversationStarted = Notification.Name("com.krabear.agent.conversationStarted")
    static let krabConversationStopped = Notification.Name("com.krabear.agent.conversationStopped")
}

// MARK: - Conversation state

/// Текущее состояние диалогового сеанса.
enum ConversationState: Equatable {
    /// Готов к запуску, нет активного соединения.
    case idle
    /// Ожидаем загрузку движка на сервере.
    case connecting
    /// Движок готов, слушаем пользователя.
    case listening
    /// AI обрабатывает запрос.
    case thinking
    /// AI произносит ответ (TTS).
    case speaking
    /// Произошла ошибка.
    case error(String)

    static func == (lhs: ConversationState, rhs: ConversationState) -> Bool {
        switch (lhs, rhs) {
        case (.idle, .idle),
             (.connecting, .connecting),
             (.listening, .listening),
             (.thinking, .thinking),
             (.speaking, .speaking):
            return true
        case (.error(let a), .error(let b)):
            return a == b
        default:
            return false
        }
    }

    var localizedLabel: String {
        switch self {
        case .idle:                  return "⚪ Готов"
        case .connecting:            return "🟡 Подключение..."
        case .listening:             return "🟢 Слушает"
        case .thinking:              return "🟡 Думает"
        case .speaking:              return "🔴 Говорит"
        case .error(let msg):        return "🔴 Ошибка: \(msg)"
        }
    }
}

// MARK: - ConversationViewController

@MainActor
final class ConversationViewController: NSViewController {

    // MARK: State
    var conversationState: ConversationState = .idle {
        didSet { applyState(conversationState) }
    }

    /// Конфигурация текущей сессии (URL, движок, язык).
    var config: ConversationConfig

    // MARK: UI elements (constructed in +UI extension)
    let statusLabel         = NSTextField(labelWithString: "⚪ Готов")
    let transcriptView      = NSTextView()
    let waveformPlaceholder = NSView()
    let startButton         = ThemePrimaryButton(title: "🎙 Начать разговор", target: nil, action: nil)
    let interruptButton     = ThemeSecondaryButton(title: "Прервать AI", target: nil, action: nil)
    let settingsDrawer      = NSStackView()
    let langHintSelector    = NSPopUpButton(frame: .zero, pullsDown: false)
    let engineSelector      = NSPopUpButton(frame: .zero, pullsDown: false)
    let brainSelector       = NSPopUpButton(frame: .zero, pullsDown: false)
    let brainModeControl    = NSSegmentedControl(labels: ["Быстро", "Краб", "Авто"], trackingMode: .selectOne, target: nil, action: nil)
    let setBrainModeDefault = ThemeSecondaryButton(title: "Сделать дефолтом", target: nil, action: nil)
    let brainModeHintLabel  = NSTextField(labelWithString: "")
    let settingsDisclosure  = NSButton(checkboxWithTitle: "Настройки", target: nil, action: nil)

    // MARK: Internal
    var transcriptBuffer = ""
    var isSessionActive  = false

    /// Fallback-таймер ручного прерывания: если сервер не подтвердил conv.interrupted
    /// за interruptFallbackInterval — применяем прерывание локально.
    var interruptFallbackTimer: Timer?
    /// Интервал fallback (инжектируется в тестах; прод — 2с).
    var interruptFallbackInterval: TimeInterval = 2.0

    // MARK: - Init

    init(config: ConversationConfig) {
        self.config = config
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        self.config = .default
        super.init(coder: coder)
    }

    // MARK: - Lifecycle

    override func loadView() {
        view = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        buildUI()
        let savedIdx = ConversationViewController.brainModeSegmentValues.firstIndex(of: config.brainMode) ?? 2
        brainModeControl.selectedSegment = savedIdx
        applyState(.idle)
    }

    // MARK: - Public API (for PR 1.5: triggers — hotkey / wake word)

    /// Запустить диалоговую сессию. Вызывается из HotkeyManager (PR 1.5).
    func startConversation() {
        guard !isSessionActive else { return }
        isSessionActive = true
        NotificationCenter.default.post(name: .krabConversationStarted, object: nil)
        conversationState = .connecting
        transcriptBuffer = ""
        updateTranscript("")
        startWebSocketSession()
        startAudioCapture()
    }

    /// Остановить сессию. Вызывается из hotkey-toggle или кнопки «Прервать».
    func stopConversation() {
        guard isSessionActive else { return }
        isSessionActive = false
        interruptFallbackTimer?.invalidate()
        interruptFallbackTimer = nil
        sendControlMessage(.end)
        closeWebSocket()
        stopAudioCapture()
        conversationState = .idle
        NotificationCenter.default.post(name: .krabConversationStopped, object: nil)
    }

    /// Прервать текущее TTS-воспроизведение AI. Вызывается из кнопки «Прервать AI»
    /// (окно и overlay). Состояние переключает НЕ сам — ждёт серверного
    /// conv.interrupted (единая точка handleInterrupted); fallback через
    /// interruptFallbackInterval, если подтверждение не пришло.
    func interruptAI() {
        guard isSessionActive else { return }
        sendControlMessage(.interrupt)
        interruptFallbackTimer?.invalidate()
        interruptFallbackTimer = Timer.scheduledTimer(
            withTimeInterval: interruptFallbackInterval, repeats: false
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.isSessionActive else { return }
                AgentLogger.shared.info("[ConversationVC] Interrupt: сервер не подтвердил за \(self.interruptFallbackInterval)s — локальный fallback")
                self.handleInterrupted(reason: "local_fallback")
            }
        }
    }

    /// Единая точка обработки прерывания — из серверного conv.interrupted
    /// (голосовой barge-in ИЛИ подтверждение кнопки) и из локального fallback.
    func handleInterrupted(reason: String) {
        guard isSessionActive else { return }
        interruptFallbackTimer?.invalidate()
        interruptFallbackTimer = nil
        flushDownlinkPlayback()
        appendTranscriptLine("— Прервано")
        conversationState = .listening
    }

    // MARK: - State application

    private func applyState(_ state: ConversationState) {
        statusLabel.stringValue = state.localizedLabel

        switch state {
        case .idle, .error:
            startButton.isEnabled   = true
            startButton.title       = "🎙 Начать разговор"
            interruptButton.isHidden = true

        case .connecting, .listening, .thinking:
            startButton.isEnabled   = false
            startButton.title       = "Идёт разговор..."
            interruptButton.isHidden = true

        case .speaking:
            startButton.isEnabled    = false
            startButton.title        = "Идёт разговор..."
            interruptButton.isHidden = false
        }
    }

    // MARK: - Internal helpers called by extensions

    func updateTranscript(_ text: String) {
        transcriptView.string = text
        // Прокрутить вниз к последней строке.
        transcriptView.scrollToEndOfDocument(nil)
    }

    func appendTranscriptLine(_ line: String) {
        if transcriptBuffer.isEmpty {
            transcriptBuffer = line
        } else {
            transcriptBuffer += "\n" + line
        }
        updateTranscript(transcriptBuffer)
    }

    func handleDownlinkEvent(_ event: ConversationEvent) {
        switch event {
        case .sttPartial(let text, _, let isFinal):
            if isFinal {
                appendTranscriptLine("Вы: \(text)")
                conversationState = .thinking
            } else {
                // Показываем частичный текст без сохранения в буфере.
                updateTranscript(transcriptBuffer + (transcriptBuffer.isEmpty ? "" : "\n") + "Вы: \(text)…")
                if conversationState == .connecting || conversationState == .listening {
                    conversationState = .listening
                }
            }

        case .engineLoaded(let name, let elapsed):
            let elapsedStr = elapsed > 0 ? " (\(String(format: "%.1f", elapsed))s)" : ""
            appendTranscriptLine("— Движок \(name) готов\(elapsedStr)")
            conversationState = .listening

        case .replyFinal(let text):
            appendTranscriptLine("AI: \(text)")
            conversationState = .speaking

        case .recycled(let reason):
            let msg = reason.isEmpty ? "Сессия перезапущена" : "Сессия перезапущена: \(reason)"
            appendTranscriptLine("— \(msg)")
            conversationState = .error(msg)
            stopConversation()

        case .closed:
            // Штатное закрытие сервером — завершаем без error-состояния, сразу (не ждём WS-дропа).
            appendTranscriptLine("— Сессия завершена сервером")
            stopConversation()

        case .error(let code, let message):
            appendTranscriptLine("— Ошибка [\(code)]: \(message)")
            conversationState = .error(message)
            stopConversation()

        case .interrupted(let reason):
            AgentLogger.shared.info("[ConversationVC] conv.interrupted (\(reason))")
            handleInterrupted(reason: reason)

        case .unknown(let type, _):
            // Неизвестный тип события (conv.vad_*, conv.audio_chunk) — логируем, не падаем.
            AgentLogger.shared.info("[ConversationVC] Неизвестное событие: \(type)")
        }
    }

    // MARK: - Button actions

    @objc func onStartStopTapped() {
        if isSessionActive {
            stopConversation()
        } else {
            startConversation()
        }
    }

    @objc func onInterruptTapped() {
        interruptAI()
    }

    @objc func onSettingsDisclosureTapped() {
        KrabEarTheme.Motion.animate(
            duration: KrabEarTheme.Motion.Duration.short,
            easing:   KrabEarTheme.Motion.Easing.easeOut
        ) {
            self.settingsDrawer.isHidden = (self.settingsDisclosure.state == .off)
        }
    }
}
