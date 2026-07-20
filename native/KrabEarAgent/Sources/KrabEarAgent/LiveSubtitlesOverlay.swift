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
    var _testHasActiveSSETask: Bool { sseStreamTask != nil }

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

    private var sseStreamTask: LiveSubtitlesSSETask?
    private let sseTaskFactory: ((URLRequest) -> LiveSubtitlesSSETask)?

    // MARK: - UserDefaults keys

    private let positionKey = "KrabEar_LiveSubsHUDPosition"
    private let showOrigKey = "KrabEar_LiveSubsShowOriginal"

    // MARK: - Init

    override convenience init() {
        self.init(sseTaskFactory: nil)
    }

    /// Фабрика подменяется только в тестах; обычный путь создаёт URLSessionDataTask.
    init(sseTaskFactory: ((URLRequest) -> LiveSubtitlesSSETask)?) {
        // Создаём плавающий NSPanel
        let initialFrame = NSRect(x: 0, y: 0, width: 680, height: 120)
        panel = NSPanel(
            contentRect: initialFrame,
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        self.sseTaskFactory = sseTaskFactory
        super.init()
        setupPanel()
        restorePosition()

        // Восстановить настройку showOriginal
        if UserDefaults.standard.object(forKey: showOrigKey) != nil {
            showOriginalAndTranslation = UserDefaults.standard.bool(forKey: showOrigKey)
        }
    }

    // MARK: - Public API

    func show() {
        panel.orderFront(nil)
        isVisible = true
        showListeningIndicator()
        startSSE()
        startNoResultsTimer()
    }

    func hide() {
        panel.orderOut(nil)
        isVisible = false
        stopSSE()
        cancelNoResultsTimer()
        clearAll()
    }

    func resetPosition() {
        UserDefaults.standard.removeObject(forKey: positionKey)
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
        if let saved = UserDefaults.standard.string(forKey: positionKey),
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
            UserDefaults.standard.set(str, forKey: positionKey)
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
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=live_subs.result") else { return }
        // Реальный SSE через delegate-based session
        startSSEStream(url: url)
    }

    private func stopSSE() {
        sseStreamTask?.cancel()
        sseStreamTask = nil
        pendingSSEEventType = nil
    }

    // MARK: - SSE Stream (URLSession streaming)

    private lazy var sseDelegate: SSESessionDelegate = SSESessionDelegate { [weak self] line in
        Task { @MainActor [weak self] in
            self?.handleSSELine(line)
        }
    }
    private lazy var sseStreamSession = URLSession(configuration: .default, delegate: sseDelegate, delegateQueue: nil)

    private func startSSEStream(url: URL) {
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let task = sseTaskFactory?(request) ?? sseStreamSession.dataTask(with: request)
        sseStreamTask = task
        task.resume()
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
