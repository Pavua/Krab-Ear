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

    // MARK: - Whisper (подсказка скринеру, инцидент 2026-09-26)
    // Поле ввода и кнопка отправки живут ОТДЕЛЬНЫМ рядом под транскриптом, а
    // не в хедере: хедер держит инвариант `test_panel_header_layout_no_overlap`
    // (title + state-бейдж + cost + пикер + две кнопки) и не должен расти.
    private let whisperBar = NSStackView()
    private let whisperField = NSTextField()
    private let whisperSendButton = ThemeButton()
    private let whisperStatusLabel = NSTextField(labelWithString: "")
    /// Отправленная подсказка ждёт ТОЛЬКО ответа VG (ack по WS в v1 не слушаем,
    /// brief §4) — держим кнопку выключенной до completion, иначе владелец
    /// влепит вторую подсказку поверх первой.
    private var whisperInFlight = false
    private var whisperStatusClearWork: DispatchWorkItem?

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
        // Whisper-бар ТОЛЬКО для скрининга (brief §2): у перевода/prompt-call
        // подсказка агенту уже есть на iOS, дублировать поле нельзя.
        setWhisperFieldVisible(session.isScreening)
        // State-бейдж (live/terminal) задаёт ТОЛЬКО координатор — showPanel его не трогает,
        // иначе открытие панели по терминальной сессии перетёрло бы setTerminal.
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        refreshSessionPicker()
    }

    func updateTranscript(_ entries: [TranscriptEntry]) {
        transcriptStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        for entry in entries.suffix(200) {  // рендер-кап; полный буфер держит координатор
            let item = row(for: entry)
            transcriptStack.addArrangedSubview(item)
            item.widthAnchor.constraint(equalTo: transcriptStack.widthAnchor, constant: -(KrabEarTheme.Metrics.cardPadding * 2)).isActive = true
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
        // Звонок мёртв — подсказывать больше некому: замок на время полёта
        // сбрасываем, чтобы кнопка не осталась мёртвой disabled при следующем
        // звонке (бар всё равно скрыт setWhisperFieldVisible(false) ниже).
        whisperInFlight = false
        whisperSendButton.isEnabled = false
        whisperField.isEnabled = false
        closeHangupSheetIfOpen()
    }

    func setLive() {
        stateBadge.stringValue = "в эфире"
        listenButton.isEnabled = true
        hangupButton.isEnabled = true
        whisperField.isEnabled = true
        whisperSendButton.isEnabled = !whisperInFlight
    }

    // MARK: - Whisper UI (протокол CallObserverPanelPresenting)

    func setWhisperFieldVisible(_ visible: Bool) {
        whisperBar.isHidden = !visible
        if !visible {
            // Скрытие = сессия не скрининговая: чистим и ввод, и статус, чтобы
            // при возврате панели к скринингу не мигал чужой текст/ошибка.
            whisperField.stringValue = ""
            clearWhisperStatus()
            whisperSendButton.isEnabled = false
            whisperField.isEnabled = false
        } else {
            whisperField.isEnabled = true
            whisperSendButton.isEnabled = !whisperInFlight
        }
    }

    func setWhisperSendEnabled(_ enabled: Bool) {
        whisperInFlight = !enabled
        whisperSendButton.isEnabled = enabled && !whisperBar.isHidden
    }

    func presentWhisperResult(accepted: Bool, message: String?) {
        if accepted {
            // Успех: тихий чек + поле освобождаем (brief §4).
            whisperField.stringValue = ""
            setWhisperStatus("Подсказка принята", isError: false)
            let performer: NSHapticFeedbackPerformer = NSHapticFeedbackManager.defaultPerformer
            performer.perform(.generic, performanceTime: .now)
        } else {
            // Ошибка: плашка 3с, ТЕКСТ В ПОЛЕ СОХРАНЁН (brief §4 + DoD.2).
            setWhisperStatus(message ?? "Подсказка не отправлена", isError: true)
        }
    }

    private func setWhisperStatus(_ text: String, isError: Bool) {
        whisperStatusClearWork?.cancel()
        whisperStatusClearWork = nil
        whisperStatusLabel.stringValue = text
        whisperStatusLabel.textColor = isError
            ? KrabEarTheme.Colors.warning
            : KrabEarTheme.Colors.textSecondary
        guard !text.isEmpty else { return }
        let work = DispatchWorkItem { [weak self] in
            self?.whisperStatusLabel.stringValue = ""
            self?.whisperStatusClearWork = nil
        }
        whisperStatusClearWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.whisperStatusSeconds, execute: work)
    }

    private func clearWhisperStatus() {
        whisperStatusClearWork?.cancel()
        whisperStatusClearWork = nil
        whisperStatusLabel.stringValue = ""
    }

    private static let whisperStatusSeconds: TimeInterval = 3.0

    @objc private func onWhisperSendTapped() {
        let text = whisperField.stringValue
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !whisperInFlight else { return }
        // Клавиатурный путь тот же, что и кнопка (Return в поле = отправить).
        coordinator?.userWhisperedFromPanel(text: text)
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
            lbl.isBordered = false
            lbl.drawsBackground = false
            let wrap = NSStackView(views: [lbl])
            wrap.orientation = .vertical
            wrap.alignment = .centerX
            wrap.identifier = NSUserInterfaceItemIdentifier("transcript:· \(msg)")
            wrap.translatesAutoresizingMaskIntoConstraints = false
            return wrap
        }

        let container = NSStackView()

        // Восстанавливаем точную legacy-строку для тестового хука
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
        container.translatesAutoresizingMaskIntoConstraints = false

        let bubble = NSBox()
        bubble.boxType = .custom
        bubble.borderWidth = 1.0
        bubble.borderColor = KrabEarTheme.Colors.border
        bubble.fillColor = isAgent
            ? KrabEarTheme.Colors.accent.withAlphaComponent(0.12)
            : KrabEarTheme.Colors.cardBackground
        bubble.wantsLayer = true
        bubble.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        bubble.layer?.cornerCurve = .continuous
        bubble.translatesAutoresizingMaskIntoConstraints = false

        let contentStack = NSStackView()
        contentStack.orientation = .vertical
        contentStack.alignment = isAgent ? .trailing : .leading
        contentStack.spacing = 2
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        let origLbl = NSTextField(wrappingLabelWithString: originalText)
        origLbl.font = KrabEarTheme.Typography.body
        origLbl.textColor = KrabEarTheme.Colors.textPrimary
        origLbl.isBordered = false
        origLbl.drawsBackground = false
        contentStack.addArrangedSubview(origLbl)

        if let tr = translationText {
            let trLbl = NSTextField(wrappingLabelWithString: tr)
            trLbl.font = KrabEarTheme.Typography.caption
            trLbl.textColor = KrabEarTheme.Colors.textSecondary
            trLbl.isBordered = false
            trLbl.drawsBackground = false
            contentStack.addArrangedSubview(trLbl)
        }

        if let sys = extraSystemText {
            let sysLbl = NSTextField(wrappingLabelWithString: sys)
            sysLbl.font = KrabEarTheme.Typography.caption
            sysLbl.textColor = KrabEarTheme.Colors.textDisabled
            sysLbl.isBordered = false
            sysLbl.drawsBackground = false
            contentStack.addArrangedSubview(sysLbl)
        }

        bubble.contentView = contentStack
        container.addArrangedSubview(bubble)

        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: bubble.topAnchor, constant: KrabEarTheme.Metrics.standard),
            contentStack.bottomAnchor.constraint(equalTo: bubble.bottomAnchor, constant: -KrabEarTheme.Metrics.standard),
            contentStack.leadingAnchor.constraint(equalTo: bubble.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            contentStack.trailingAnchor.constraint(equalTo: bubble.trailingAnchor, constant: -KrabEarTheme.Metrics.comfortable),
            bubble.widthAnchor.constraint(lessThanOrEqualTo: container.widthAnchor, multiplier: 0.85)
        ])

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
        listenButton.contentTintColor = KrabEarTheme.Colors.textSecondary
        listenButton.wantsLayer = true
        listenButton.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        listenButton.layer?.cornerCurve = .continuous
        listenButton.target = self
        listenButton.action = #selector(onListenTapped)

        hangupButton.image = NSImage(systemSymbolName: "phone.down.fill", accessibilityDescription: "Положить трубку")
        hangupButton.title = ""
        hangupButton.isBordered = false
        hangupButton.isTransparentStyle = true
        hangupButton.contentTintColor = KrabEarTheme.Colors.textSecondary
        hangupButton.wantsLayer = true
        hangupButton.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        hangupButton.layer?.cornerCurve = .continuous
        hangupButton.target = self
        hangupButton.action = #selector(onHangupTapped)

        NSLayoutConstraint.activate([
            listenButton.widthAnchor.constraint(equalToConstant: KrabEarTheme.Metrics.controlHeight),
            listenButton.heightAnchor.constraint(equalToConstant: KrabEarTheme.Metrics.controlHeight),
            hangupButton.widthAnchor.constraint(equalToConstant: KrabEarTheme.Metrics.controlHeight),
            hangupButton.heightAnchor.constraint(equalToConstant: KrabEarTheme.Metrics.controlHeight),
        ])

        sessionPicker.target = self
        sessionPicker.action = #selector(onSessionPicked)
        sessionPicker.isHidden = true

        costAlertLabel.textColor = KrabEarTheme.Colors.warning
        costAlertLabel.font = KrabEarTheme.Typography.captionMedium
        costAlertLabel.isHidden = true
        costLabel.textColor = KrabEarTheme.Colors.textSecondary
        costLabel.font = KrabEarTheme.Typography.captionMedium.tabular()

        stateBadge.font = KrabEarTheme.Typography.captionMedium
        stateBadge.textColor = KrabEarTheme.Colors.textPrimary
        stateBadge.isBordered = false
        stateBadge.drawsBackground = false
        stateBadge.isEditable = false
        stateBadge.isSelectable = false

        stateBadgeBox.boxType = .custom
        stateBadgeBox.borderWidth = 1.0
        stateBadgeBox.borderColor = KrabEarTheme.Colors.border
        stateBadgeBox.fillColor = KrabEarTheme.Colors.cardBackground
        stateBadgeBox.wantsLayer = true
        stateBadgeBox.layer?.cornerRadius = 10
        stateBadgeBox.layer?.cornerCurve = .continuous
        stateBadgeBox.contentView = stateBadge

        stateBadge.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stateBadge.topAnchor.constraint(equalTo: stateBadgeBox.topAnchor, constant: 2),
            stateBadge.bottomAnchor.constraint(equalTo: stateBadgeBox.bottomAnchor, constant: -2),
            stateBadge.leadingAnchor.constraint(equalTo: stateBadgeBox.leadingAnchor, constant: 8),
            stateBadge.trailingAnchor.constraint(equalTo: stateBadgeBox.trailingAnchor, constant: -8)
        ])

        let header = NSStackView(views: [inContentTitleLabel, stateBadgeBox, sessionPicker, NSView(), costAlertLabel,
                                         costLabel, listenButton, hangupButton])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.cardPadding,
            left: KrabEarTheme.Metrics.cardPadding,
            bottom: KrabEarTheme.Metrics.standard,
            right: KrabEarTheme.Metrics.cardPadding
        )
        header.spacing = KrabEarTheme.Metrics.standard
        stateBadgeBox.setContentCompressionResistancePriority(.required, for: .horizontal)

        transcriptStack.orientation = .vertical
        transcriptStack.alignment = .leading
        transcriptStack.spacing = KrabEarTheme.Metrics.standard
        transcriptStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.standard,
            left: KrabEarTheme.Metrics.cardPadding,
            bottom: KrabEarTheme.Metrics.standard,
            right: KrabEarTheme.Metrics.cardPadding
        )
        transcriptStack.translatesAutoresizingMaskIntoConstraints = false

        scrollView.applyThemeInnerScroll()
        scrollView.documentView = transcriptStack
        scrollView.hasVerticalScroller = true
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        // Whisper-бар (brief §1): однострочный ввод + отправка ПОД транскриптом,
        // отдельным рядом root-стека — хедер держит инвариант
        // test_panel_header_layout_no_overlap и не должен расти.
        whisperField.placeholderString = "Шепнуть скринеру…"
        whisperField.target = self
        whisperField.action = #selector(onWhisperSendTapped)
        whisperField.font = KrabEarTheme.Typography.body
        whisperField.isBezeled = true
        whisperField.isEnabled = false
        whisperSendButton.image = NSImage(systemSymbolName: "paperplane",
                                         accessibilityDescription: "Отправить подсказку")
        whisperSendButton.title = ""
        whisperSendButton.isBordered = false
        whisperSendButton.isTransparentStyle = true
        whisperSendButton.contentTintColor = KrabEarTheme.Colors.textSecondary
        whisperSendButton.wantsLayer = true
        whisperSendButton.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        whisperSendButton.layer?.cornerCurve = .continuous
        whisperSendButton.target = self
        whisperSendButton.action = #selector(onWhisperSendTapped)
        whisperSendButton.isEnabled = false
        NSLayoutConstraint.activate([
            whisperSendButton.widthAnchor.constraint(equalToConstant: KrabEarTheme.Metrics.controlHeight),
            whisperSendButton.heightAnchor.constraint(equalToConstant: KrabEarTheme.Metrics.controlHeight),
        ])

        whisperStatusLabel.font = KrabEarTheme.Typography.caption
        whisperStatusLabel.textColor = KrabEarTheme.Colors.textSecondary
        whisperStatusLabel.lineBreakMode = .byTruncatingTail

        let whisperRow = NSStackView(views: [whisperField, whisperSendButton])
        whisperRow.orientation = .horizontal
        whisperRow.alignment = .centerY
        whisperRow.spacing = KrabEarTheme.Metrics.tight

        whisperBar.orientation = .vertical
        whisperBar.alignment = .leading
        whisperBar.spacing = KrabEarTheme.Metrics.tight
        whisperBar.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.tight,
            left: KrabEarTheme.Metrics.comfortable,
            bottom: KrabEarTheme.Metrics.standard,
            right: KrabEarTheme.Metrics.comfortable
        )
        whisperBar.addArrangedSubview(whisperRow)
        whisperBar.addArrangedSubview(whisperStatusLabel)
        whisperBar.isHidden = true
        whisperBar.translatesAutoresizingMaskIntoConstraints = false

        let root = NSStackView(views: [header, scrollView, whisperBar])
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

    // MARK: - Тестовые хуки
    var testHook_stateBadgeText: String { stateBadge.stringValue }
    var testHook_inContentTitleLabel: NSTextField { inContentTitleLabel }
    var testHook_stateBadgeBox: NSBox { stateBadgeBox }
    /// Whisper-хуки (инцидент 2026-09-26): видимость по meta.screening,
    /// single-flight блокировка кнопки, обратная связь и сохранение текста
    /// при ошибке проверяются тестами CallObserverUITests.
    var testHook_whisperBarVisible: Bool { !whisperBar.isHidden }
    var testHook_whisperField: NSTextField { whisperField }
    var testHook_whisperSendEnabled: Bool { whisperSendButton.isEnabled && whisperField.isEnabled }
    var testHook_whisperStatusText: String { whisperStatusLabel.stringValue }
    var testHook_transcriptPlainText: String {
        transcriptStack.arrangedSubviews
            .compactMap { view -> String? in
                if let tf = view as? NSTextField { return tf.stringValue }
                // Извлечение plain text для тестов через identifier вида transcript:<текст>
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
