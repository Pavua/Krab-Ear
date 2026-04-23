/*
 LiveSubtitlesOverlay — плавающий HUD с субтитрами для System Audio Capture.

 Архитектура:
 - NSPanel (floating, always on top, non-activating) внизу экрана
 - Хранит последние 3 строки субтитров (оригинал + перевод или только перевод)
 - Auto-fade каждой строки через 4 с
 - Можно перетаскивать мышью → позиция сохраняется в UserDefaults
 - Подписывается на SSE `/v1/events?filter=live_subs.result` через URLSession

 Event payload (live_subs.result):
   { "type": "live_subs.result", "data": { "original": "...", "translation": "..." } }

 Связи:
 - SystemAudioCapture: start/stop синхронизировано
 - IPCClient: не нужен (SSE REST endpoint localhost:5005)
*/

import AppKit
import Foundation

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

    // MARK: - UI

    private let panel: NSPanel
    private let stackView = NSStackView()
    private let backdropView = NSVisualEffectView()

    // MARK: - SSE

    private var sseTask: URLSessionDataTask?
    private let sseSession = URLSession(configuration: .default)
    private var sseBuffer = ""

    // MARK: - UserDefaults keys

    private let positionKey = "KrabEar_LiveSubsHUDPosition"
    private let showOrigKey = "KrabEar_LiveSubsShowOriginal"

    // MARK: - Init

    override init() {
        // Создаём плавающий NSPanel
        let initialFrame = NSRect(x: 0, y: 0, width: 680, height: 120)
        panel = NSPanel(
            contentRect: initialFrame,
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered,
            defer: false
        )
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
        startSSE()
    }

    func hide() {
        panel.orderOut(nil)
        isVisible = false
        stopSSE()
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

        // Backdrop blur
        backdropView.material = .hudWindow
        backdropView.blendingMode = .behindWindow
        backdropView.state = .active
        backdropView.wantsLayer = true
        backdropView.layer?.cornerRadius = 12
        backdropView.layer?.masksToBounds = true
        backdropView.layer?.borderWidth = 0.5
        backdropView.layer?.borderColor = NSColor.white.withAlphaComponent(0.18).cgColor

        // StackView для строк субтитров
        stackView.orientation = .vertical
        stackView.spacing = 4
        stackView.alignment = .centerX
        stackView.edgeInsets = NSEdgeInsets(top: 10, left: 16, bottom: 10, right: 16)
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

    private func restorePosition() {
        if let saved = UserDefaults.standard.string(forKey: positionKey),
           let data = saved.data(using: .utf8),
           let dict = try? JSONSerialization.jsonObject(with: data) as? [String: CGFloat],
           let x = dict["x"], let y = dict["y"] {
            let size = panel.frame.size
            panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: false)
        } else {
            placeAtBottom()
        }
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
        rebuildSubtitleViews()
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
            ? .systemFont(ofSize: 15, weight: .semibold)
            : .systemFont(ofSize: 11, weight: .regular)
        label.textColor = isTranslation
            ? .white
            : NSColor.white.withAlphaComponent(0.65)
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
        sseTask?.cancel()
        sseTask = nil
        sseBuffer = ""
    }

    // MARK: - SSE Stream (URLSession streaming)

    private var sseStreamTask: URLSessionDataTask?
    private lazy var sseDelegate: SSESessionDelegate = SSESessionDelegate { [weak self] line in
        Task { @MainActor [weak self] in
            self?.handleSSELine(line)
        }
    }
    private lazy var sseStreamSession = URLSession(configuration: .default, delegate: sseDelegate, delegateQueue: nil)

    private func startSSEStream(url: URL) {
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let task = sseStreamSession.dataTask(with: request)
        sseStreamTask = task
        task.resume()
    }

    // MARK: - SSE Line Parsing

    private func handleSSELine(_ line: String) {
        if line.hasPrefix("data: ") {
            let json = String(line.dropFirst(6))
            parseSSEData(json)
        }
    }

    private func parseSSEData(_ json: String) {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let eventData = obj["data"] as? [String: Any] ?? obj
        let original = (eventData["original"] as? String) ?? ""
        let translation = (eventData["translation"] as? String)
            ?? (eventData["translated"] as? String)
            ?? ""
        guard !translation.isEmpty || !original.isEmpty else { return }
        addEntry(original: original, translation: translation)
    }
}

// MARK: - SSESessionDelegate

/// URLSessionDataDelegate для SSE long-poll стриминга.
private final class SSESessionDelegate: NSObject, URLSessionDataDelegate {
    private let onLine: (String) -> Void
    private var buffer = ""

    init(onLine: @escaping (String) -> Void) {
        self.onLine = onLine
        super.init()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer += String(decoding: data, as: UTF8.self)
        // Разбиваем по \n, отправляем полные строки
        let lines = buffer.components(separatedBy: "\n")
        buffer = lines.last ?? ""
        for line in lines.dropLast() {
            onLine(line)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        // SSE соединение закрылось — не перезапускаем (stop() уже вызван)
    }
}
