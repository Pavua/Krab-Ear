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

    /// Локальная озвучка ошибок (Волна 3c). Реальный speak инжектится из +VoiceTab;
    /// без инжекции — тихая text-only деградация.
    let errorAnnouncer = ConversationErrorAnnouncer()

    /// Плавающий статус-HUD (Волна 3c). Создаётся лениво при старте сессии.
    var statusOverlay: ConversationStatusOverlay?
    /// Токены наблюдателей фокуса окна (живут до конца жизни VC — таб постоянный).
    private var windowFocusObservers: [NSObjectProtocol] = []

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

        // Волна 3c: HUD показывается, когда окно теряет фокус во время сессии.
        // VC живёт всю жизнь приложения (постоянный таб) — наблюдатели не снимаем.
        let nc = NotificationCenter.default
        for name in [NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification] {
            windowFocusObservers.append(
                nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                    Task { @MainActor in self?.updateOverlayVisibility() }
                }
            )
        }

        // Укрепление code-review батча 3: явный willClose-наблюдатель — defence-in-depth,
        // НАМЕРЕННО дублирующий транзитивный путь HistoryPanelController.windowWillClose()
        // (~строка 2520) → conversationVC?.stopConversation(). Тот путь работает сегодня,
        // но спека называла наблюдатель `willClose` обязанностью ИМЕННО этого класса, а
        // транзитивный путь недокументирован и хрупок к будущему рефакторингу той строки.
        // stopConversation() идемпотентен (guard isSessionActive) — если транзитивный путь
        // сработал первым, повторный вызов отсюда безопасен, дублирования эффекта не будет.
        windowFocusObservers.append(
            nc.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { [weak self] note in
                Task { @MainActor in
                    guard let self else { return }
                    // Обе стороны ОБЯЗАНЫ быть non-nil ДО identity-сравнения: у двух optional
                    // nil === nil истинно в Swift, а значит закрытие ЛЮБОГО чужого окна в
                    // приложении ложно триггернуло бы stopConversation, пока у этого VC ещё
                    // нет собственного window (например до вставки view в иерархию).
                    guard let ourWindow = self.view.window,
                          let closedWindow = note.object as? NSWindow,
                          closedWindow === ourWindow else { return }
                    self.stopConversation()
                }
            }
        )
    }

    // MARK: - Public API (for PR 1.5: triggers — hotkey / wake word)

    /// Запустить диалоговую сессию. Вызывается из HotkeyManager (PR 1.5).
    func startConversation() {
        guard !isSessionActive else { return }
        isSessionActive = true
        ensureStatusOverlay()
        updateOverlayVisibility()
        NotificationCenter.default.post(name: .krabConversationStarted, object: nil)
        conversationState = .connecting
        transcriptBuffer = ""
        updateTranscript("")
        // Микрофон запускается только после `conv.ready`: Moshi требует 24 кГц,
        // а старый pipeline — 16 кГц. До согласования отправлять фреймы нельзя.
        prepareAudioNegotiation()
        startWebSocketSession()
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
        updateOverlayVisibility()
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
    /// Идемпотентна по состоянию: прерывание осмысленно ТОЛЬКО пока ассистент
    /// реально отвечает (.speaking/.thinking). После fallback состояние уже
    /// .listening — запоздавшее серверное подтверждение (или двойное
    /// conv.interrupted подряд) отсекается гардом, без дубля «— Прервано».
    /// Голосовой barge-in от VG приходит только в compute/playback-фазах
    /// (= клиентские .thinking/.speaking) — штатный путь гард не ломает.
    func handleInterrupted(reason: String) {
        guard isSessionActive,
              conversationState == .speaking || conversationState == .thinking else { return }
        interruptFallbackTimer?.invalidate()
        interruptFallbackTimer = nil
        flushDownlinkPlayback()
        appendTranscriptLine("— Прервано")
        conversationState = .listening
    }

    /// Классифицировать провал WS по текущему состоянию и озвучить.
    /// .connecting = не смогли подключиться; иначе — обрыв посреди сессии.
    func classifyAndAnnounceWSFailure() {
        let cls: ConversationErrorAnnouncer.ErrorClass =
            (conversationState == .connecting) ? .gatewayUnreachable : .connectionLost
        errorAnnouncer.announce(cls)
    }

    // MARK: - Status overlay (Волна 3c)

    /// Правило видимости HUD: сессия активна И окно не в фокусе.
    static func shouldShowOverlay(sessionActive: Bool, windowIsKey: Bool) -> Bool {
        sessionActive && !windowIsKey
    }

    /// Создать overlay при первом обращении и подвязать кнопку «Прервать».
    func ensureStatusOverlay() {
        guard statusOverlay == nil else { return }
        let overlay = ConversationStatusOverlay()
        overlay.onInterrupt = { [weak self] in self?.interruptAI() }
        statusOverlay = overlay
    }

    /// Пересчитать видимость HUD (вызывается из start/stop, applyState и фокус-наблюдателей).
    func updateOverlayVisibility() {
        guard let overlay = statusOverlay else { return }
        let windowIsKey = view.window?.isKeyWindow ?? false
        if ConversationViewController.shouldShowOverlay(sessionActive: isSessionActive, windowIsKey: windowIsKey) {
            if !overlay.isVisible { overlay.show() }
        } else {
            if overlay.isVisible { overlay.hide() }
        }
    }

    // MARK: - State application

    private func applyState(_ state: ConversationState) {
        statusLabel.stringValue = state.localizedLabel
        statusOverlay?.update(state: state)

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

        case .engineReady(let name, let elapsed, let sampleRate):
            let elapsedStr = elapsed > 0 ? " (\(String(format: "%.1f", elapsed))s)" : ""
            appendTranscriptLine("— Движок \(name) готов\(elapsedStr)")
            activateNegotiatedAudio(sampleRate: sampleRate)
            conversationState = .listening

        case .engineLoaded(let name, let elapsed):
            let elapsedStr = elapsed > 0 ? " (\(String(format: "%.1f", elapsed))s)" : ""
            appendTranscriptLine("— Движок \(name) готов\(elapsedStr)")
            // Старый Gateway не передавал sample_rate и работал на 16 кГц.
            activateNegotiatedAudio(sampleRate: nil)
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
            // Гонка со «Стоп»: conv.error может быть уже в полёте (receive-callback
            // принял байты), когда юзер остановил сессию — Task исполняется ПОСЛЕ
            // stopConversation(). Инвариант: юзерская остановка не озвучивается,
            // UI не откатывается в .error после осознанного Стоп.
            guard isSessionActive else { return }
            appendTranscriptLine("— Ошибка [\(code)]: \(message)")
            errorAnnouncer.announce(.serverError)
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
