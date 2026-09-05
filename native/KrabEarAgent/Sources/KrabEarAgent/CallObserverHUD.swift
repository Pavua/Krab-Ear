import AppKit

/// Плавающая плашка звонка (spec §3 комп. 4; паттерн LiveSubtitlesOverlay).
/// Клик (mouseUp без движения) → разворот в панель; кнопки — SF Symbols
/// (класс AGENT-J/M — никаких Unicode-глифов в тайтлах).
final class CallObserverHUD: NSObject, CallObserverHUDPresenting {
    weak var coordinator: CallObserverCoordinator?

    private var panel: NSPanel?
    private let statusDot = NSBox()
    private let statusLabel = NSTextField(labelWithString: "")
    private let badgesLabel = NSTextField(labelWithString: "")
    private let linesLabel = NSTextField(wrappingLabelWithString: "")
    private let listenButton = ThemeButton()
    private let hangupButton = ThemeButton()
    private let closeButton = ThemeButton()
    private var elapsedTimer: Timer?
    private var callCreatedAt: Date?
    private var buttonActions: [ObjectIdentifier: () -> Void] = [:]

    var isHUDVisible: Bool { panel?.isVisible ?? false }

    static func isClick(down: NSPoint, up: NSPoint) -> Bool {
        hypot(up.x - down.x, up.y - down.y) < 4.0
    }

    func showHUD(session: VGSessionInfo) {
        if panel == nil { buildPanel() }
        // L3 (w1 final, sibling-асимметрия): VG отдаёт ISO и с долями секунды, и
        // без — общий VGSessionWatcher.parseISO уже принимает оба формата, свой
        // одноразовый ISO8601DateFormatter() здесь ловил только один из них.
        callCreatedAt = VGSessionWatcher.parseISO(session.createdAt)
        
        let caller = session.phone.isEmpty ? session.id : session.phone
        if session.isScreening {
            let didText = session.forwardedFrom.isEmpty ? "" : " на \(session.forwardedFrom)"
            statusLabel.stringValue = "Скрининг входящего · \(caller)\(didText)"
        } else {
            statusLabel.stringValue = "\(session.callDirection) \(caller)"
        }
        
        // I-4 (координатор): ЛЮБОЙ showHUD — включая повторный для уже видимого
        // HUD — обязан очистить ранее показанный linger-текст.
        linesLabel.stringValue = "· ждём реплик…"
        linesLabel.textColor = KrabEarTheme.Colors.textSecondary
        startElapsedTimer()
        panel?.orderFrontRegardless()
    }

    func updateHUD(session: VGSessionInfo, status: String, lastEntries: [TranscriptEntry],
                   listenState: CallAudioPlayer.ListenState, listeningSessionId: String?) {
        let statusLower = status.lowercased()
        if statusLower.contains("ring") {
            statusDot.fillColor = .systemYellow
        } else if statusLower.contains("end") {
            statusDot.fillColor = .systemGray
        } else {
            statusDot.fillColor = .systemGreen
        }
        
        var badges = [String]()
        if statusLower.contains("mute") { badges.append("mute") }
        if statusLower.contains("hold") { badges.append("hold") }
        badgesLabel.stringValue = badges.isEmpty ? "" : "· " + badges.joined(separator: " ")
        badgesLabel.isHidden = badges.isEmpty

        if lastEntries.isEmpty {
            linesLabel.stringValue = "· ждём реплик…"
            linesLabel.textColor = KrabEarTheme.Colors.textSecondary
        } else {
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
            linesLabel.textColor = KrabEarTheme.Colors.textPrimary
        }
        // T9 (2г): зелёный индикатор — только если реально слушаем ИМЕННО эту
        // карточку (listeningSessionId может отставать от hudTrackedId).
        let isListeningThisCall = listeningSessionId == session.id && listenState == .listening
        listenButton.contentTintColor = isListeningThisCall ? .systemGreen : KrabEarTheme.Colors.textSecondary
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
                        styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
                        backing: .buffered, defer: false)
        p.level = .floating
        p.isMovableByWindowBackground = true
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hidesOnDeactivate = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.hasShadow = true

        let backdrop = HUDBackdropView()
        let content = HUDClickView()
        content.onClick = { [weak self] in self?.coordinator?.userExpandedHUD() }
        content.translatesAutoresizingMaskIntoConstraints = false

        statusDot.boxType = .custom
        statusDot.borderType = .noBorder
        statusDot.wantsLayer = true
        statusDot.layer?.cornerRadius = 4
        NSLayoutConstraint.activate([
            statusDot.widthAnchor.constraint(equalToConstant: 8),
            statusDot.heightAnchor.constraint(equalToConstant: 8)
        ])

        statusLabel.textColor = KrabEarTheme.Colors.textPrimary
        statusLabel.font = KrabEarTheme.Typography.captionMedium
        badgesLabel.textColor = KrabEarTheme.Colors.textSecondary
        badgesLabel.font = KrabEarTheme.Typography.captionMedium
        
        linesLabel.textColor = KrabEarTheme.Colors.textSecondary
        linesLabel.font = KrabEarTheme.Typography.body
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
        buttons.spacing = 2
        
        let statusStack = NSStackView(views: [statusDot, statusLabel, badgesLabel])
        statusStack.orientation = .horizontal
        statusStack.spacing = 6
        statusStack.alignment = .centerY
        
        let topRow = NSStackView(views: [statusStack, NSView(), buttons])
        topRow.orientation = .horizontal
        topRow.distribution = .fill
        topRow.alignment = .centerY

        let stack = NSStackView(views: [topRow, linesLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 16, bottom: 12, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false
        
        content.addSubview(stack)
        backdrop.addSubview(content)
        
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: backdrop.topAnchor),
            content.bottomAnchor.constraint(equalTo: backdrop.bottomAnchor),
            content.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            topRow.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32) // account for insets
        ])
        p.contentView = backdrop
        panel = p
    }

    private func configure(_ button: ThemeButton, symbol: String, accessibility: String,
                           action: @escaping () -> Void) {
        // 🔴 assumeIsolated, а не @MainActor на методе: свойства ThemeButton
        // (isTransparentStyle и др.) main-actor-изолированы, а протокол
        // CallObserverHUDPresenting — нет, и пометка каскадом ушла бы в
        // координатор. Инвариант, на котором это держится: ЕДИНСТВЕННЫЙ путь
        // сюда — showHUD() → buildPanel(), а showHUD зовёт координатор строго
        // с main (все его delegate/stream-колбэки перепрыгивают на main).
        // Нарушение инварианта = fatalError, а не тихая гонка — это осознанно.
        MainActor.assumeIsolated {
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: accessibility)
            button.title = ""
            button.isBordered = false
            button.isTransparentStyle = true
            button.contentTintColor = KrabEarTheme.Colors.textSecondary
            self.buttonActions[ObjectIdentifier(button)] = action
            button.target = self
            button.action = #selector(self.buttonTapped(_:))
        }
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
    
    required init?(coder: NSCoder) { fatalError() }
    
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
