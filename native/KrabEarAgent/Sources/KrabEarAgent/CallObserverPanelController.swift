import AppKit

/// Полное окно звонка (spec §3 комп. 5; каркас — MeetingLivePanelController,
/// но источник данных — WS-клиент координатора, НЕ SSE). Терминал НЕ закрывает
/// окно: транскрипт — единственная копия (§3 комп. 5).
final class CallObserverPanelController: NSWindowController, CallObserverPanelPresenting {
    weak var coordinator: CallObserverCoordinator?

    private let inContentTitleLabel = NSTextField(labelWithString: "Звонок агента")
    private let stateBadgeBox = NSBox()
    private let stateBadge = NSTextField(labelWithString: "")
    private let costLabel = NSTextField(labelWithString: "—")
    /// MED-4 (w1 final): липкий cost-alert бейдж — ОТДЕЛЬНОЕ поле от costLabel
    /// (которое перетирается периодическим cost-поллером координатора).
    private let costAlertLabel = NSTextField(labelWithString: "")
    private let listenButton = ThemeButton()
    private let hangupButton = ThemeButton()
    private let transcriptStack = NSStackView()
    private let scrollView = NSScrollView()
    private let sessionPicker = NSPopUpButton()
    private var hangupSheetOpen = false

    var isPanelVisible: Bool { window?.isVisible ?? false }

    convenience init() {
        let win = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 520, height: 560),
                           styleMask: [.titled, .closable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "Звонок агента"
        self.init(window: win)
        buildUI()
    }

    func showPanel(session: VGSessionInfo) {
        let titleText: String
        let caller = session.phone.isEmpty ? session.id : session.phone
        if session.isScreening {
            let didText = session.forwardedFrom.isEmpty ? "" : " (на \(session.forwardedFrom))"
            titleText = "Скрининг входящего · \(caller)\(didText)"
        } else {
            titleText = "Звонок агента · \(caller)"
        }
        window?.title = titleText
        inContentTitleLabel.stringValue = titleText
        // State-бейдж (live/terminal) задаёт ТОЛЬКО координатор — showPanel его не трогает,
        // иначе открытие панели по терминальной сессии перетёрло бы setTerminal.
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        refreshSessionPicker()
    }

    func updateTranscript(_ entries: [TranscriptEntry]) {
        transcriptStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        for entry in entries.suffix(200) {  // рендер-кап; полный буфер держит координатор
            transcriptStack.addArrangedSubview(row(for: entry))
        }
        if let doc = scrollView.documentView {
            doc.scroll(NSPoint(x: 0, y: doc.bounds.maxY))
        }
    }

    func updateStatus(status: String, muted: Bool?, held: Bool?, badge: String?) {
        var parts = [status]
        if muted == true { parts.append("mute") }
        if held == true { parts.append("hold") }
        if let badge { parts.append(badge) }
        stateBadge.stringValue = parts.joined(separator: " · ")
    }

    func updateCost(_ text: String) { costLabel.stringValue = text }

    func setCostAlert(_ text: String?) {
        costAlertLabel.stringValue = text ?? ""
        costAlertLabel.isHidden = text?.isEmpty ?? true
    }

    func setTerminal(message: String) {
        stateBadge.stringValue = message
        listenButton.isEnabled = false
        hangupButton.isEnabled = false
        closeHangupSheetIfOpen()
    }

    func setLive() {
        stateBadge.stringValue = "в эфире"
        listenButton.isEnabled = true
        hangupButton.isEnabled = true
    }

    func closeHangupSheetIfOpen() {
        guard hangupSheetOpen, let window, let sheet = window.attachedSheet else { return }
        window.endSheet(sheet, returnCode: .cancel)
        hangupSheetOpen = false
    }

    func presentHangupConfirm() { onHangupTapped() }

    // HIGH-1 (w1 final): панельная кнопка целится строго в selectedId — она не
    // смеет угонять выбор панели на hudTrackedId (см. coordinator doc-comment).
    @objc private func onListenTapped() { coordinator?.userToggledListenFromPanel() }

    @objc private func onHangupTapped() {
        guard let window, !hangupSheetOpen else { return }
        hangupSheetOpen = true
        let alert = NSAlert()
        alert.messageText = "Положить трубку?"
        alert.informativeText = "Звонок агента будет завершён."
        alert.addButton(withTitle: "Положить трубку")
        alert.addButton(withTitle: "Отмена")
        presentAlertSheet(alert, for: window) { [weak self] response in
            self?.hangupSheetOpen = false
            if response == .alertFirstButtonReturn {
                self?.coordinator?.userRequestedHangupConfirmed()
            }
        }
    }

    private func row(for entry: TranscriptEntry) -> NSView {
        let isAgent: Bool
        let originalText: String
        var translationText: String? = nil
        var extraSystemText: String? = nil
        
        switch entry.kind {
        case .remote(let text, let translation):
            isAgent = false
            originalText = text
            translationText = translation
        case .agent(let text, let ru, _, let interrupted, let spoken, let fraction):
            isAgent = true
            if interrupted {
                let pct = fraction.map { Int($0 * 100) } ?? 0
                originalText = spoken ?? text
                extraSystemText = "[прервано \(pct) %]"
            } else {
                originalText = text
                translationText = ru
            }
        case .system(let msg):
            let lbl = NSTextField(labelWithString: "· \(msg)")
            lbl.font = KrabEarTheme.Typography.captionMedium
            lbl.textColor = KrabEarTheme.Colors.textSecondary
            let wrap = NSStackView(views: [lbl])
            wrap.alignment = .centerX
            wrap.identifier = NSUserInterfaceItemIdentifier("transcript:· \(msg)")
            return wrap
        }
        
        let container = NSStackView()
        
        // Reconstruct the exact legacy string for the test hook
        let legacyText: String
        switch entry.kind {
        case .remote(let text, let translation):
            legacyText = translation.map { "Собеседник: \(text)\n  → \($0)" } ?? "Собеседник: \(text)"
        case .agent(let text, let ru, _, let interrupted, let spoken, let fraction):
            if interrupted {
                let pct = fraction.map { Int($0 * 100) } ?? 0
                legacyText = "Агент: \(spoken ?? text) [прервано \(pct) %]"
            } else {
                legacyText = ru.map { "Агент: \(text)\n  → \($0)" } ?? "Агент: \(text)"
            }
        case .system(let msg):
            legacyText = "· \(msg)"
        }
        container.identifier = NSUserInterfaceItemIdentifier("transcript:" + legacyText)
        
        container.orientation = .vertical
        container.alignment = isAgent ? .trailing : .leading
        
        let bubble = NSBox()
        bubble.boxType = .custom
        bubble.borderType = .noBorder
        bubble.fillColor = isAgent ? KrabEarTheme.Colors.accent.withAlphaComponent(0.15) : KrabEarTheme.Colors.cardBackground
        bubble.wantsLayer = true
        bubble.layer?.cornerRadius = 10
        
        let contentStack = NSStackView()
        contentStack.orientation = .vertical
        contentStack.alignment = isAgent ? .trailing : .leading
        contentStack.spacing = 2
        
        let origLbl = NSTextField(wrappingLabelWithString: originalText)
        origLbl.font = KrabEarTheme.Typography.body
        origLbl.textColor = KrabEarTheme.Colors.textPrimary
        contentStack.addArrangedSubview(origLbl)
        
        if let tr = translationText {
            let trLbl = NSTextField(wrappingLabelWithString: tr)
            trLbl.font = KrabEarTheme.Typography.caption
            trLbl.textColor = KrabEarTheme.Colors.textSecondary
            contentStack.addArrangedSubview(trLbl)
        }
        
        if let sys = extraSystemText {
            let sysLbl = NSTextField(wrappingLabelWithString: sys)
            sysLbl.font = KrabEarTheme.Typography.caption
            sysLbl.textColor = KrabEarTheme.Colors.textDisabled
            contentStack.addArrangedSubview(sysLbl)
        }
        
        bubble.contentView = contentStack
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: bubble.topAnchor, constant: 8),
            contentStack.bottomAnchor.constraint(equalTo: bubble.bottomAnchor, constant: -8),
            contentStack.leadingAnchor.constraint(equalTo: bubble.leadingAnchor, constant: 12),
            contentStack.trailingAnchor.constraint(equalTo: bubble.trailingAnchor, constant: -12)
        ])
        
        container.addArrangedSubview(bubble)
        return container
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }
        if let win = window { KrabEarTheme.applyTheme(to: win) }

        // Как MeetingLivePanel / QuickCapture: `.titled` даёт крестик,
        // titleVisibility=.hidden убирает native title из прозрачного titlebar
        // (иначе «Звонок агента · +номер» рисуется поверх бейджа).
        window?.titleVisibility = .hidden
        
        inContentTitleLabel.font = KrabEarTheme.Typography.sectionTitle
        inContentTitleLabel.textColor = KrabEarTheme.Colors.textPrimary
        inContentTitleLabel.lineBreakMode = .byTruncatingTail
        inContentTitleLabel.maximumNumberOfLines = 1
        inContentTitleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        inContentTitleLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)
        
        listenButton.image = NSImage(systemSymbolName: "speaker.wave.2", accessibilityDescription: "Слушать")
        listenButton.title = ""
        listenButton.isBordered = false
        listenButton.isTransparentStyle = true
        listenButton.target = self
        listenButton.action = #selector(onListenTapped)
        
        hangupButton.image = NSImage(systemSymbolName: "phone.down.fill", accessibilityDescription: "Положить трубку")
        hangupButton.title = ""
        hangupButton.isBordered = false
        hangupButton.isTransparentStyle = true
        hangupButton.target = self
        hangupButton.action = #selector(onHangupTapped)

        sessionPicker.target = self
        sessionPicker.action = #selector(onSessionPicked)
        sessionPicker.isHidden = true
        
        costAlertLabel.textColor = KrabEarTheme.Colors.warning
        costAlertLabel.font = KrabEarTheme.Typography.captionMedium
        costAlertLabel.isHidden = true
        costLabel.textColor = KrabEarTheme.Colors.textSecondary
        costLabel.font = KrabEarTheme.Typography.captionMedium

        stateBadge.font = KrabEarTheme.Typography.captionMedium
        stateBadge.textColor = KrabEarTheme.Colors.textPrimary
        
        stateBadgeBox.boxType = .custom
        stateBadgeBox.borderType = .lineBorder
        stateBadgeBox.borderColor = KrabEarTheme.Colors.border
        stateBadgeBox.fillColor = KrabEarTheme.Colors.cardBackground
        stateBadgeBox.wantsLayer = true
        stateBadgeBox.layer?.cornerRadius = 6
        stateBadgeBox.contentView = stateBadge
        
        stateBadge.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stateBadge.topAnchor.constraint(equalTo: stateBadgeBox.topAnchor, constant: 2),
            stateBadge.bottomAnchor.constraint(equalTo: stateBadgeBox.bottomAnchor, constant: -2),
            stateBadge.leadingAnchor.constraint(equalTo: stateBadgeBox.leadingAnchor, constant: 6),
            stateBadge.trailingAnchor.constraint(equalTo: stateBadgeBox.trailingAnchor, constant: -6)
        ])

        let header = NSStackView(views: [inContentTitleLabel, stateBadgeBox, sessionPicker, NSView(), costAlertLabel,
                                         costLabel, listenButton, hangupButton])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.edgeInsets = NSEdgeInsets(top: 12, left: 16, bottom: 8, right: 16)
        header.spacing = 8
        stateBadgeBox.setContentCompressionResistancePriority(.required, for: .horizontal)

        transcriptStack.orientation = .vertical
        transcriptStack.alignment = .leading
        transcriptStack.spacing = 6
        transcriptStack.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        transcriptStack.translatesAutoresizingMaskIntoConstraints = false

        scrollView.documentView = transcriptStack
        scrollView.hasVerticalScroller = true
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        let root = NSStackView(views: [header, scrollView])
        root.orientation = .vertical
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        
        let topAnchor = (window?.contentLayoutGuide as? NSLayoutGuide)?.topAnchor ?? content.topAnchor
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scrollView.widthAnchor.constraint(equalTo: root.widthAnchor),
            // ⚠️ Без этого transcriptStack растягивается по контенту до нулевой
            // ширины внутри clip view (MeetingLivePanelController itemsStack —
            // тот же обязательный пин к ширине своего NSScrollView).
            transcriptStack.widthAnchor.constraint(equalTo: scrollView.widthAnchor),
        ])
        window?.delegate = self
    }

    /// Пикер >1 одновременных звонков (spec §3.1); скрыт при единственной сессии.
    func refreshSessionPicker() {
        let sessions = coordinator?.observedSessions() ?? []
        sessionPicker.isHidden = sessions.count <= 1
        sessionPicker.removeAllItems()
        for (id, label) in sessions {
            sessionPicker.addItem(withTitle: label)
            sessionPicker.lastItem?.representedObject = id
        }
    }

    @objc private func onSessionPicked() {
        guard let id = sessionPicker.selectedItem?.representedObject as? String else { return }
        coordinator?.userSelectedSession(id)
    }

    // MARK: Test hooks
    var testHook_stateBadgeText: String { stateBadge.stringValue }
    var testHook_inContentTitleLabel: NSTextField { inContentTitleLabel }
    var testHook_stateBadgeBox: NSBox { stateBadgeBox }
    var testHook_transcriptPlainText: String {
        transcriptStack.arrangedSubviews
            .compactMap { view -> String? in
                if let tf = view as? NSTextField { return tf.stringValue } // fallback for .system(let msg) which returns a StackView now? No, wait.
                // Let's recursively find the hidden 'accessibilityLabel' or reconstruct it.
                // Better: we can store the plain text in `view.toolTip` or `view.identifier`.
                if let sv = view as? NSStackView, let identifier = sv.identifier?.rawValue, identifier.starts(with: "transcript:") {
                    return String(identifier.dropFirst(11))
                }
                return nil
            }
            .joined(separator: "\n")
    }
}

extension CallObserverPanelController: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) {
        coordinator?.userClosedPanel()
    }
}
