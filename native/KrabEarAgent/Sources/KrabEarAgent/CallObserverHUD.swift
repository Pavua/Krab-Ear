import AppKit

/// Плавающая плашка звонка (spec §3 комп. 4; паттерн LiveSubtitlesOverlay).
/// Клик (mouseUp без движения) → разворот в панель; кнопки — SF Symbols
/// (класс AGENT-J/M — никаких Unicode-глифов в тайтлах).
final class CallObserverHUD: NSObject, CallObserverHUDPresenting {
    weak var coordinator: CallObserverCoordinator?

    private var panel: NSPanel?
    private let statusLabel = NSTextField(labelWithString: "")
    private let linesLabel = NSTextField(wrappingLabelWithString: "")
    private let listenButton = NSButton()
    private let hangupButton = NSButton()
    private let closeButton = NSButton()
    private var elapsedTimer: Timer?
    private var callCreatedAt: Date?
    private var buttonActions: [ObjectIdentifier: () -> Void] = [:]

    var isHUDVisible: Bool { panel?.isVisible ?? false }

    static func isClick(down: NSPoint, up: NSPoint) -> Bool {
        hypot(up.x - down.x, up.y - down.y) < 4.0
    }

    func showHUD(session: VGSessionInfo) {
        if panel == nil { buildPanel() }
        callCreatedAt = ISO8601DateFormatter().date(from: session.createdAt)
        statusLabel.stringValue = "\(session.callDirection) \(session.phone)"
        // I-4 (координатор): ЛЮБОЙ showHUD — включая повторный для уже видимого
        // HUD — обязан очистить ранее показанный linger-текст.
        linesLabel.stringValue = ""
        startElapsedTimer()
        panel?.orderFrontRegardless()
    }

    func updateHUD(session: VGSessionInfo, status: String, lastEntries: [TranscriptEntry],
                   listenState: CallAudioPlayer.ListenState, listeningSessionId: String?) {
        let lines = lastEntries.map { entry -> String in
            switch entry.kind {
            case .remote(let text, let tr):
                return tr.map { "Он: \(text) / \($0)" } ?? "Он: \(text)"
            case .agent(let text, let ru, _, let interrupted, let spoken, _):
                let shown = interrupted ? (spoken ?? text) + " …" : text
                return ru.map { "Агент: \(shown) / \($0)" } ?? "Агент: \(shown)"
            case .system(let msg):
                return "· \(msg)"
            }
        }
        linesLabel.stringValue = lines.joined(separator: "\n")
        // T9 (2г): зелёный индикатор — только если реально слушаем ИМЕННО эту
        // карточку (listeningSessionId может отставать от hudTrackedId).
        let isListeningThisCall = listeningSessionId == session.id && listenState == .listening
        listenButton.contentTintColor = isListeningThisCall ? .systemGreen : nil
        listenButton.toolTip = listenState == .subscriberLimit
            ? "Лимит слушателей VG — попробуйте ещё раз" : "Слушать звонок"
    }

    func showLinger(message: String) {
        statusLabel.stringValue = message
        elapsedTimer?.invalidate()
    }

    func hideHUD() {
        elapsedTimer?.invalidate()
        panel?.orderOut(nil)
    }

    private func startElapsedTimer() {
        elapsedTimer?.invalidate()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self, let created = self.callCreatedAt else { return }
            let s = Int(Date().timeIntervalSince(created))
            let mmss = String(format: "%02d:%02d", s / 60, s % 60)
            var text = self.statusLabel.stringValue
            if let dotRange = text.range(of: " · ") { text = String(text[..<dotRange.lowerBound]) }
            self.statusLabel.stringValue = text + " · " + mmss
        }
    }

    private func buildPanel() {
        let p = NSPanel(contentRect: NSRect(x: 120, y: 120, width: 340, height: 96),
                        styleMask: [.nonactivatingPanel, .borderless, .utilityWindow],
                        backing: .buffered, defer: false)
        p.level = .floating
        p.isMovableByWindowBackground = true
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hidesOnDeactivate = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let content = HUDClickView()
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.72).cgColor
        content.layer?.cornerRadius = 12
        content.onClick = { [weak self] in self?.coordinator?.userExpandedHUD() }

        statusLabel.textColor = .white
        statusLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        linesLabel.textColor = NSColor.white.withAlphaComponent(0.85)
        linesLabel.font = .systemFont(ofSize: 11)
        linesLabel.maximumNumberOfLines = 3

        configure(listenButton, symbol: "speaker.wave.2", accessibility: "Слушать") { [weak self] in
            self?.coordinator?.userToggledListen()
        }
        configure(hangupButton, symbol: "phone.down.fill", accessibility: "Положить трубку") { [weak self] in
            self?.coordinator?.userRequestedHangupFromHUD()
        }
        configure(closeButton, symbol: "xmark", accessibility: "Скрыть") { [weak self] in
            self?.coordinator?.userClosedHUD()
        }

        let buttons = NSStackView(views: [listenButton, hangupButton, closeButton])
        buttons.orientation = .horizontal
        let stack = NSStackView(views: [statusLabel, linesLabel, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
        ])
        p.contentView = content
        panel = p
    }

    private func configure(_ button: NSButton, symbol: String, accessibility: String,
                           action: @escaping () -> Void) {
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: accessibility)
        button.title = ""
        button.bezelStyle = .circular
        button.setButtonType(.momentaryPushIn)
        buttonActions[ObjectIdentifier(button)] = action
        button.target = self
        button.action = #selector(buttonTapped(_:))
    }

    @objc private func buttonTapped(_ sender: NSButton) {
        buttonActions[ObjectIdentifier(sender)]?()
    }

    // MARK: Test hooks
    var testHook_listenButton: NSButton { listenButton }
    var testHook_hangupButton: NSButton { hangupButton }
}

/// Клик-vs-драг: mouseUp < 4pt от mouseDown = клик (isMovableByWindowBackground
/// перехватывает драг сам, но короткий mouseDown/Up доходит).
private final class HUDClickView: NSView {
    var onClick: (() -> Void)?
    private var downPoint: NSPoint?

    override func mouseDown(with event: NSEvent) {
        downPoint = event.locationInWindow
        super.mouseDown(with: event)
    }

    override func mouseUp(with event: NSEvent) {
        if let down = downPoint,
           CallObserverHUD.isClick(down: down, up: event.locationInWindow) {
            onClick?()
        }
        downPoint = nil
        super.mouseUp(with: event)
    }
}
