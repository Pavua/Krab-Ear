import AppKit

/// Полное окно звонка (spec §3 комп. 5; каркас — MeetingLivePanelController,
/// но источник данных — WS-клиент координатора, НЕ SSE). Терминал НЕ закрывает
/// окно: транскрипт — единственная копия (§3 комп. 5).
final class CallObserverPanelController: NSWindowController, CallObserverPanelPresenting {
    weak var coordinator: CallObserverCoordinator?

    private let stateBadge = NSTextField(labelWithString: "")
    private let costLabel = NSTextField(labelWithString: "—")
    /// MED-4 (w1 final): липкий cost-alert бейдж — ОТДЕЛЬНОЕ поле от costLabel
    /// (которое перетирается периодическим cost-поллером координатора).
    private let costAlertLabel = NSTextField(labelWithString: "")
    private let listenButton = NSButton()
    private let hangupButton = NSButton()
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
        window?.title = "Звонок агента · \(session.phone.isEmpty ? session.id : session.phone)"
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
        let label = NSTextField(wrappingLabelWithString: "")
        label.font = .systemFont(ofSize: 12)
        switch entry.kind {
        case .remote(let text, let translation):
            label.stringValue = translation.map { "Собеседник: \(text)\n  → \($0)" } ?? "Собеседник: \(text)"
        case .agent(let text, let ru, _, let interrupted, let spoken, let fraction):
            if interrupted {
                let pct = fraction.map { Int($0 * 100) } ?? 0
                label.stringValue = "Агент: \(spoken ?? text) [прервано \(pct) %]"
                label.textColor = .secondaryLabelColor
            } else {
                label.stringValue = ru.map { "Агент: \(text)\n  → \($0)" } ?? "Агент: \(text)"
            }
        case .system(let msg):
            label.stringValue = "· \(msg)"
            label.textColor = .secondaryLabelColor
        }
        return label
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }
        listenButton.image = NSImage(systemSymbolName: "speaker.wave.2", accessibilityDescription: "Слушать")
        listenButton.title = ""
        listenButton.target = self
        listenButton.action = #selector(onListenTapped)
        hangupButton.image = NSImage(systemSymbolName: "phone.down.fill", accessibilityDescription: "Положить трубку")
        hangupButton.title = ""
        hangupButton.target = self
        hangupButton.action = #selector(onHangupTapped)

        sessionPicker.target = self
        sessionPicker.action = #selector(onSessionPicked)
        sessionPicker.isHidden = true
        costAlertLabel.textColor = .systemOrange
        costAlertLabel.font = .boldSystemFont(ofSize: 12)
        costAlertLabel.isHidden = true
        let header = NSStackView(views: [stateBadge, sessionPicker, NSView(), costAlertLabel,
                                         costLabel, listenButton, hangupButton])
        header.orientation = .horizontal
        header.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 4, right: 12)

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
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor),
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
    var testHook_transcriptPlainText: String {
        transcriptStack.arrangedSubviews
            .compactMap { ($0 as? NSTextField)?.stringValue }
            .joined(separator: "\n")
    }
}

extension CallObserverPanelController: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) {
        coordinator?.userClosedPanel()
    }
}
