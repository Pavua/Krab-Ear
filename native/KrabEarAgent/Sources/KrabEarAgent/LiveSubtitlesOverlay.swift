/*
 LiveSubtitlesOverlay — плавающий HUD с субтитрами для System Audio Capture.

 Архитектура:
 - NSPanel (floating, always on top, non-activating) внизу экрана
 - Хранит последние 3 строки субтитров (оригинал + перевод или только перевод)
 - Auto-fade каждой строки через 4 с
 - Можно перетаскивать мышью → позиция сохраняется в UserDefaults
 - Подписывается на SSE `/v1/events?filter=live_subs.result` через URLSession

 Event payload (live_subs.result) — сервер шлёт плоский data без вложенного "data":
   event: live_subs.result
   data: { "text": "...", "translation": "...", "start_ts": ..., "end_ts": ..., "language_detected": "..." }
 Парсер поддерживает и "original" и "text" для обратной совместимости.

 Связи:
 - SystemAudioCapture: start/stop синхронизировано
 - IPCClient: не нужен (SSE REST endpoint localhost:5005)
*/

import AppKit
import Foundation

/// Минимальный контракт SSE-задачи нужен, чтобы жизненный цикл соединения можно было
/// проверить без реального REST-сервера. В рабочем коде его реализует URLSessionDataTask.
protocol LiveSubtitlesSSETask: AnyObject {
    func resume()
    func cancel()
}

extension URLSessionDataTask: LiveSubtitlesSSETask {}

/// Контракт SSE-сессии отделяет владение URLSession от панели и позволяет
/// детерминированно проверять её инвалидизацию при hide/deinit.
protocol LiveSubtitlesSSESession: AnyObject {
    func makeLiveSubtitlesTask(with request: URLRequest) -> LiveSubtitlesSSETask
    func invalidateAndCancel()
}

extension URLSession: LiveSubtitlesSSESession {
    func makeLiveSubtitlesTask(with request: URLRequest) -> LiveSubtitlesSSETask {
        dataTask(with: request)
    }
}

// MARK: - SubtitleEntry

private struct SubtitleEntry: Identifiable {
    let id = UUID()
    let original: String
    let translation: String
    let timestamp: Date
}

// MARK: - LiveSubtitlesOverlay

@MainActor
final class LiveSubtitlesOverlay: NSObject {

    // MARK: - Configuration

    /// Показывать оригинал и перевод (true) или только перевод (false).
    var showOriginalAndTranslation: Bool = true {
        didSet { rebuildSubtitleViews() }
    }

    /// REST base URL (порт 5005 Flask сервера)
    var restBaseURL: String = "http://127.0.0.1:5005"

    // MARK: - State

    private(set) var isVisible = false
    private var entries: [SubtitleEntry] = []
    private let maxEntries = 3
    private var fadeTimers: [UUID: Timer] = [:]

    // MARK: - Test hooks (accessible via @testable import in unit tests)

    /// Количество активных subtitle-записей (для unit-тестов).
    var _testEntryCount: Int { entries.count }

    /// Уровень окна панели (для unit-тестов).
    var _testPanelLevel: NSWindow.Level { panel.level }

    /// isMovableByWindowBackground (для unit-тестов).
    var _testPanelIsDraggable: Bool { panel.isMovableByWindowBackground }

    /// Origin панели (для unit-тестов off-screen guard).
    var _testPanelOrigin: NSPoint { panel.frame.origin }

    /// Есть ли ровно одна принадлежащая панели активная SSE-задача.
    var _testHasActiveSSETask: Bool { sseConnection != nil }

    /// Верхняя граница последовательных переподключений без полученных строк.
    var _testMaximumReconnectAttempts: Int { maxReconnectAttempts }

    /// Текущий event type из SSE (из строки "event: ..."), чтобы фильтровать data-строки.
    private var pendingSSEEventType: String? = nil

    /// Таймер "нет результатов 30 сек" — подсказка пользователю.
    private var noResultsTimer: Timer? = nil
    private let noResultsTimeout: TimeInterval = 30.0

    // MARK: - UI

    private let panel: NSPanel
    private let stackView = NSStackView()
    private let backdropView = HUDBackdropView()

    // MARK: - SSE

    private struct SSEConnection {
        let id: UUID
        let generation: UInt64
        // Явное владение делегатом делает его срок жизни равным сроку соединения
        // даже для тестовых реализаций сессии, которые не обязаны его удерживать.
        let delegate: SSESessionDelegate
        let session: LiveSubtitlesSSESession
        let task: LiveSubtitlesSSETask
    }

    private var sseConnection: SSEConnection?
    private var sseGeneration: UInt64 = 0
    private var reconnectAttempts = 0
    private let maxReconnectAttempts = 5
    private let reconnectBaseDelay: TimeInterval = 0.5
    private let reconnectMaximumDelay: TimeInterval = 8.0
    private var reconnectWorkItem: DispatchWorkItem?
    private var reconnectToken: UUID?
    private let sseSessionFactory: ((SSESessionDelegate) -> LiveSubtitlesSSESession)?
    private let reconnectScheduler: ((TimeInterval, DispatchWorkItem) -> Void)?

    // MARK: - UserDefaults keys

    private let positionKey = "KrabEar_LiveSubsHUDPosition"
    private let showOrigKey = "KrabEar_LiveSubsShowOriginal"
    /// Хранилище настроек HUD; production использует `.standard`, тесты — UUID-suite.
    private let userDefaults: UserDefaults
    /// Граница видимости окна: production вызывает AppKit, тесты используют no-op.
    private let panelOrdering: PanelOrdering

    // MARK: - Init

    override convenience init() {
        self.init(sseSessionFactory: nil, reconnectScheduler: nil, userDefaults: .standard)
    }

    /// Упрощённая тестовая точка входа без создания собственной SSE-фабрики.
    convenience init(
        userDefaults: UserDefaults,
        panelOrdering: PanelOrdering = AppKitPanelOrdering()
    ) {
        self.init(
            sseSessionFactory: nil,
            reconnectScheduler: nil,
            userDefaults: userDefaults,
            panelOrdering: panelOrdering
        )
    }

    /// Фабрики подменяются только в тестах; обычный путь использует URLSession
    /// и ограниченную экспоненциальную задержку на главной очереди.
    init(
        sseSessionFactory: ((SSESessionDelegate) -> LiveSubtitlesSSESession)?,
        reconnectScheduler: ((TimeInterval, DispatchWorkItem) -> Void)? = nil,
        userDefaults: UserDefaults = .standard,
        panelOrdering: PanelOrdering = AppKitPanelOrdering()
    ) {
        // Создаём плавающий NSPanel
        let initialFrame = NSRect(x: 0, y: 0, width: 680, height: 120)
        panel = NSPanel(
            contentRect: initialFrame,
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        self.sseSessionFactory = sseSessionFactory
        self.reconnectScheduler = reconnectScheduler
        self.userDefaults = userDefaults
        self.panelOrdering = panelOrdering
        super.init()
        setupPanel()
        restorePosition()

        // Восстановить настройку showOriginal
        if userDefaults.object(forKey: showOrigKey) != nil {
            showOriginalAndTranslation = userDefaults.bool(forKey: showOrigKey)
        }
    }

    deinit {
        reconnectWorkItem?.cancel()
        sseConnection?.task.cancel()
        sseConnection?.session.invalidateAndCancel()
    }

    // MARK: - Public API

    func show() {
        panelOrdering.orderFront(panel)
        isVisible = true
        showListeningIndicator()
        startSSE()
        startNoResultsTimer()
    }

    func hide() {
        panelOrdering.orderOut(panel)
        isVisible = false
        stopSSE()
        cancelNoResultsTimer()
        clearAll()
    }

    func resetPosition() {
        userDefaults.removeObject(forKey: positionKey)
        placeAtBottom()
    }

    // MARK: - Panel setup

    private func setupPanel() {
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
        panel.alphaValue = 0.95

        // Backdrop blur config is now in HUDBackdropView

        // StackView для строк субтитров
        stackView.orientation = .vertical
        stackView.spacing = KrabEarTheme.Metrics.tight
        stackView.alignment = .centerX
        stackView.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.comfortable,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.comfortable,
            right: KrabEarTheme.Metrics.spacious
        )
        stackView.translatesAutoresizingMaskIntoConstraints = false

        backdropView.addSubview(stackView)
        NSLayoutConstraint.activate([
            stackView.topAnchor.constraint(equalTo: backdropView.topAnchor),
            stackView.leadingAnchor.constraint(equalTo: backdropView.leadingAnchor),
            stackView.trailingAnchor.constraint(equalTo: backdropView.trailingAnchor),
            stackView.bottomAnchor.constraint(equalTo: backdropView.bottomAnchor),
        ])

        backdropView.frame = panel.contentView!.bounds
        backdropView.autoresizingMask = [.width, .height]
        panel.contentView = backdropView

        // Drag to reposition
        let drag = NSPanGestureRecognizer(target: self, action: #selector(handleDrag(_:)))
        backdropView.addGestureRecognizer(drag)
    }

    // MARK: - Position management

    private func placeAtBottom() {
        guard let screen = NSScreen.main else { return }
        let sw = screen.visibleFrame.width
        let sx = screen.visibleFrame.minX
        let sy = screen.visibleFrame.minY
        let pw: CGFloat = 680
        let ph: CGFloat = 120
        let x = sx + (sw - pw) / 2
        let y = sy + 40  // 40pt от нижнего края
        panel.setFrame(NSRect(x: x, y: y, width: pw, height: ph), display: true)
    }

    /// Валидна ли candidate-позиция — хотя бы 80% площади пересекается с visibleFrame
    /// какого-нибудь ТЕКУЩЕГО экрана. Портировано из
    /// RealtimeOverlayController.restoreSavedPosition() (M2, ~строки 757-782): без этой
    /// проверки отключение второго монитора навсегда прячет панель за экраном — сохранённая
    /// позиция ссылается на уже не существующий экран, а UserDefaults применяется безусловно.
    private func isOnScreen(_ candidate: NSRect) -> Bool {
        let totalArea = candidate.width * candidate.height
        guard totalArea > 0 else { return false }
        return NSScreen.screens.contains { screen in
            let intersection = candidate.intersection(screen.visibleFrame)
            let coveredArea = intersection.width * intersection.height
            return coveredArea / totalArea >= 0.80
        }
    }

    private func restorePosition() {
        if let saved = userDefaults.string(forKey: positionKey),
           let data = saved.data(using: .utf8),
           let dict = try? JSONSerialization.jsonObject(with: data) as? [String: CGFloat],
           let x = dict["x"], let y = dict["y"] {
            let size = panel.frame.size
            let candidate = NSRect(x: x, y: y, width: size.width, height: size.height)
            if isOnScreen(candidate) {
                panel.setFrame(candidate, display: false)
                return
            }
        }
        // Нет сохранённой позиции ИЛИ она вне видимых экранов (guard выше) — дефолт.
        placeAtBottom()
    }

    private func savePosition() {
        let origin = panel.frame.origin
        let dict: [String: CGFloat] = ["x": origin.x, "y": origin.y]
        if let data = try? JSONSerialization.data(withJSONObject: dict),
           let str = String(data: data, encoding: .utf8) {
            userDefaults.set(str, forKey: positionKey)
        }
    }

    @objc private func handleDrag(_ gr: NSPanGestureRecognizer) {
        if gr.state == .ended || gr.state == .changed {
            savePosition()
        }
    }

    // MARK: - Subtitle management

    /// Добавляет новую строку субтитров, удаляет самые старые если > maxEntries.
    func addEntry(original: String, translation: String) {
        // Убираем listening indicator / no-results hint при первом реальном результате
        if entries.isEmpty {
            cancelNoResultsTimer()
        }
        let entry = SubtitleEntry(original: original, translation: translation, timestamp: Date())
        entries.append(entry)

        // Ограничиваем до 3 последних
        if entries.count > maxEntries {
            let removed = entries.removeFirst()
            fadeTimers[removed.id]?.invalidate()
            fadeTimers.removeValue(forKey: removed.id)
        }

        rebuildSubtitleViews()
        scheduleFade(for: entry)
    }

    private func scheduleFade(for entry: SubtitleEntry) {
        let id = entry.id
        let timer = Timer.scheduledTimer(withTimeInterval: 4.0, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.removeEntry(id: id)
            }
        }
        fadeTimers[id] = timer
    }

    private func removeEntry(id: UUID) {
        fadeTimers[id]?.invalidate()
        fadeTimers.removeValue(forKey: id)
        entries.removeAll { $0.id == id }
        rebuildSubtitleViews()
    }

    func clearAll() {
        fadeTimers.values.forEach { $0.invalidate() }
        fadeTimers = [:]
        entries = []
        pendingSSEEventType = nil
        rebuildSubtitleViews()
    }

    // MARK: - Listening indicator

    /// Показывает placeholder "Слушаю..." до первого результата.
    private func showListeningIndicator() {
        // Удаляем старый контент
        stackView.arrangedSubviews.forEach { stackView.removeArrangedSubview($0); $0.removeFromSuperview() }
        
        let container = NSStackView()
        container.orientation = .horizontal
        container.spacing = KrabEarTheme.Metrics.tight
        container.alignment = .centerY
        
        let pulse = PulseIndicatorView(frame: NSRect(x: 0, y: 0, width: 8, height: 8))
        pulse.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            pulse.widthAnchor.constraint(equalToConstant: 8),
            pulse.heightAnchor.constraint(equalToConstant: 8)
        ])
        pulse.startPulsing()
        
        let label = NSTextField(labelWithString: "Слушаю...")
        label.font = KrabEarTheme.Typography.captionMedium
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.alignment = .center
        label.isBordered = false
        label.drawsBackground = false
        
        container.addArrangedSubview(pulse)
        container.addArrangedSubview(label)
        
        stackView.addArrangedSubview(container)
    }

    /// Убирает listening indicator и показывает hint об источнике аудио.
    private func showNoResultsHint() {
        stackView.arrangedSubviews.forEach { stackView.removeArrangedSubview($0); $0.removeFromSuperview() }
        let label = NSTextField(labelWithString: "Не распознано речи\n(проверь Privacy → Screen Recording\nили выбери другой источник аудио)")
        label.font = KrabEarTheme.Typography.caption
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.alignment = .center
        label.maximumNumberOfLines = 3
        label.isBordered = false
        label.drawsBackground = false
        stackView.addArrangedSubview(label)
    }

    /// Старт таймера 30 сек — если нет результатов, показать hint.
    private func startNoResultsTimer() {
        cancelNoResultsTimer()
        noResultsTimer = Timer.scheduledTimer(withTimeInterval: noResultsTimeout, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self = self, self.isVisible, self.entries.isEmpty else { return }
                self.showNoResultsHint()
            }
        }
    }

    private func cancelNoResultsTimer() {
        noResultsTimer?.invalidate()
        noResultsTimer = nil
    }

    // MARK: - Rebuild UI

    private func rebuildSubtitleViews() {
        // Удаляем старые view
        stackView.arrangedSubviews.forEach { stackView.removeArrangedSubview($0); $0.removeFromSuperview() }

        for entry in entries {
            if showOriginalAndTranslation && !entry.original.isEmpty {
                let origLabel = makeLabel(entry.original, isTranslation: false)
                stackView.addArrangedSubview(origLabel)
            }
            if !entry.translation.isEmpty {
                let transLabel = makeLabel(entry.translation, isTranslation: true)
                stackView.addArrangedSubview(transLabel)
            }
        }
    }

    private func makeLabel(_ text: String, isTranslation: Bool) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = isTranslation
            ? KrabEarTheme.Typography.display
            : KrabEarTheme.Typography.body
        label.textColor = isTranslation
            ? KrabEarTheme.Colors.textPrimary
            : KrabEarTheme.Colors.textSecondary
        label.alignment = .center
        label.lineBreakMode = .byTruncatingTail
        label.cell?.truncatesLastVisibleLine = true
        label.maximumNumberOfLines = 2
        label.isBordered = false
        label.drawsBackground = false
        label.translatesAutoresizingMaskIntoConstraints = false
        return label
    }

    // MARK: - SSE Subscription

    private func startSSE() {
        stopSSE()
        reconnectAttempts = 0
        connectSSE()
    }

    private func stopSSE() {
        // Новое поколение инвалидирует уже поставленные в MainActor обработчики.
        sseGeneration &+= 1
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        reconnectToken = nil
        reconnectAttempts = 0
        closeCurrentSSEConnection()
        pendingSSEEventType = nil
    }

    // MARK: - SSE Stream (URLSession streaming)

    private func connectSSE() {
        guard isVisible, sseConnection == nil else { return }
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=live_subs.result") else { return }

        sseGeneration &+= 1
        let generation = sseGeneration
        let connectionID = UUID()
        let delegate = SSESessionDelegate(
            onLine: { [weak self] line in
                Task { @MainActor [weak self] in
                    self?.receiveSSELine(
                        line,
                        connectionID: connectionID,
                        generation: generation
                    )
                }
            },
            onComplete: { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.completeSSEConnection(
                        connectionID: connectionID,
                        generation: generation
                    )
                }
            }
        )

        let session: LiveSubtitlesSSESession
        if let sseSessionFactory {
            session = sseSessionFactory(delegate)
        } else {
            session = URLSession(
                configuration: .default,
                delegate: delegate,
                delegateQueue: nil
            )
        }

        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let task = session.makeLiveSubtitlesTask(with: request)
        sseConnection = SSEConnection(
            id: connectionID,
            generation: generation,
            delegate: delegate,
            session: session,
            task: task
        )
        pendingSSEEventType = nil
        task.resume()
    }

    private func receiveSSELine(
        _ line: String,
        connectionID: UUID,
        generation: UInt64
    ) {
        guard isVisible,
              let connection = sseConnection,
              connection.id == connectionID,
              connection.generation == generation,
              sseGeneration == generation else { return }

        // Любая полная строка доказывает, что подключение ожило: следующая серия
        // ошибок снова получает весь бюджет переподключений.
        reconnectAttempts = 0
        handleSSELine(line)
    }

    private func completeSSEConnection(connectionID: UUID, generation: UInt64) {
        guard let connection = sseConnection,
              connection.id == connectionID,
              connection.generation == generation,
              sseGeneration == generation else { return }

        closeCurrentSSEConnection()
        pendingSSEEventType = nil
        guard isVisible else { return }
        scheduleSSEReconnect(afterGeneration: generation)
    }

    private func closeCurrentSSEConnection() {
        guard let connection = sseConnection else { return }
        // Сначала убираем идентификатор: синхронное завершение после отмены уже устарело.
        sseConnection = nil
        connection.task.cancel()
        connection.session.invalidateAndCancel()
    }

    private func scheduleSSEReconnect(afterGeneration generation: UInt64) {
        guard reconnectAttempts < maxReconnectAttempts else { return }

        let exponent = min(reconnectAttempts, 4)
        let delay = min(
            reconnectBaseDelay * pow(2.0, Double(exponent)),
            reconnectMaximumDelay
        )
        reconnectAttempts += 1

        let token = UUID()
        reconnectToken = token
        let workItem = DispatchWorkItem { [weak self] in
            Task { @MainActor [weak self] in
                guard let self,
                      self.reconnectToken == token,
                      self.sseGeneration == generation,
                      self.isVisible,
                      self.sseConnection == nil else { return }
                self.reconnectToken = nil
                self.reconnectWorkItem = nil
                self.connectSSE()
            }
        }
        reconnectWorkItem = workItem

        if let reconnectScheduler {
            reconnectScheduler(delay, workItem)
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
        }
    }

    // MARK: - SSE Line Parsing

    /// Internal SSE line handler — exposed for unit tests via @testable import.
    func _testHandleSSELine(_ line: String) {
        handleSSELine(line)
    }

    private func handleSSELine(_ line: String) {
        if line.hasPrefix("event: ") {
            // Трекаем тип события — фильтруем только live_subs.result
            pendingSSEEventType = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        } else if line.hasPrefix("data: ") {
            // Фильтр: игнорируем чужие event types
            guard pendingSSEEventType == "live_subs.result" else {
                pendingSSEEventType = nil
                return
            }
            pendingSSEEventType = nil
            let json = String(line.dropFirst(6))
            parseSSEData(json)
        } else if line.isEmpty {
            // Пустая строка — разделитель SSE-блоков, сбрасываем буферный тип
            pendingSSEEventType = nil
        }
    }

    private func parseSSEData(_ json: String) {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        // Поддерживаем оба формата:
        // 1. {type, data: {original/text, translation}} — конверт
        // 2. {original/text, translation} — плоский (сервер шлёт data-only без вложенного "data")
        let eventData = obj["data"] as? [String: Any] ?? obj
        // "original" — историческое поле; "text" — поле LiveSubsResult из Python-бэкенда
        let original = (eventData["original"] as? String)
            ?? (eventData["text"] as? String)
            ?? ""
        let translation = (eventData["translation"] as? String)
            ?? (eventData["translated"] as? String)
            ?? ""
        guard !translation.isEmpty || !original.isEmpty else { return }
        addEntry(original: original, translation: translation)
    }
}


// MARK: - Visual Helpers

private class HUDBackdropView: NSVisualEffectView {
    private let bgLayer = CALayer()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        material = .popover
        blendingMode = .behindWindow
        state = .active
        
        layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        layer?.cornerCurve = .continuous
        layer?.masksToBounds = true
        layer?.borderWidth = 1.0
        
        bgLayer.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        layer?.insertSublayer(bgLayer, at: 0)
        
        updateColors()
    }
    
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    
    override func layout() {
        super.layout()
        bgLayer.frame = bounds
    }
    
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateColors()
    }
    
    private func updateColors() {
        layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        bgLayer.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
    }
}

private class PulseIndicatorView: NSView {
    private let pulseLayer = CALayer()
    
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        pulseLayer.cornerRadius = 4
        layer?.addSublayer(pulseLayer)
        updateColors()
    }
    
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    
    override func layout() {
        super.layout()
        pulseLayer.frame = bounds
    }
    
    func startPulsing() {
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        let animation = CABasicAnimation(keyPath: "opacity")
        animation.fromValue = 1.0
        animation.toValue = 0.3
        animation.duration = KrabEarTheme.Motion.Duration.long
        animation.autoreverses = true
        animation.repeatCount = .infinity
        animation.timingFunction = KrabEarTheme.Motion.Easing.easeInOut
        pulseLayer.add(animation, forKey: "pulse")
    }
    
    func stopPulsing() {
        pulseLayer.removeAnimation(forKey: "pulse")
    }
    
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateColors()
    }
    
    private func updateColors() {
        pulseLayer.backgroundColor = KrabEarTheme.Colors.success.cgColor
    }
}

// MARK: - SSESessionDelegate (extracted в SSESessionDelegate.swift, shared с TranslationStreamView)
