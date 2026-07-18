/*
 QuickCapturePanelController — C3b Task 1: плавающая панель-скретчпад для
 быстрой голосовой заметки (C3a, Cmd+Shift+N, main+QuickCapture.swift).

 До этой волны быстрая заметка работала «вслепую» (только хоткей + звук +
 тост) — эта панель даёт визуальную обратную связь: статус записи, live-текст
 партиалов и список последних заметок с быстрым «Копировать».

 Task 1 — каркас панели + чистый рендер + test-hooks. Проводка (показ панели
 по месту вызова, поставка SSE-партиалов/списка заметок из backend'а) — Task 2,
 main+QuickCapturePanel.swift (ещё не существует). До Task 2 панель никем не
 инстанцируется в проде — `ipcClient`/`onToggleRecording` инжектируются
 владельцем ПОСЛЕ конструирования, паттерн идентичен `ipcClient`/`onFinished`
 в MeetingLivePanelController.

 Panel-boilerplate — портирован ≥80% из MeetingLivePanelController.swift
 (styleMask/level/collectionBehavior/isReleasedWhenClosed/drag/savePosition/
 restorePosition/isOnScreen≥80%, включая ревью №4 «header-таймер не должен
 тикать за закрытой панелью») с новым positionKey.

 Глиф-гейт (AGENT-J/M, CLAUDE.md): ⏺/■ из плана — черновая нотация задачи,
 НЕ буквальные Unicode-литералы в коде (0 хитов в native/Sources до этой
 волны) — статус записи рендерится тем же `RecordingIndicator` (цветная точка
 с pulsing-анимацией), что и MeetingLivePanelController; кнопка старт/стоп —
 SF Symbols `record.circle.fill`/`stop.circle.fill`.
*/

import AppKit
import Foundation

@MainActor
final class QuickCapturePanelController: NSObject, NSWindowDelegate {

    private let panel: NSPanel
    private let positionKey = "KrabEar_QuickCapturePanelPosition"

    /// Публичное (без `_test`-префикса) окно — план Task 1 проверяет
    /// styleMask/isReleasedWhenClosed/level напрямую из теста, без обёрток.
    var window: NSPanel? { panel }

    // MARK: - Данные (инжектируются владельцем в Task 2)

    /// Off-main IPC (AGENT-3) для «→ Notes». nil до инъекции владельцем —
    /// кнопка тогда просто отказывает тостом, не крашится.
    var ipcClient: IPCClient?
    /// Зовётся из ⏺/■ — владелец (main+QuickCapturePanel.swift, Task 2)
    /// подключит к AgentAppDelegate.onQuickCaptureToggle(). Панель НЕ хранит
    /// прямую ссылку на AgentAppDelegate — тот же паттерн инъекции колбэка,
    /// что onFinished в MeetingLivePanelController.
    var onToggleRecording: (() -> Void)?

    private(set) var isRecording = false
    private var startedAt: TimeInterval?
    private var timerTick: Timer?

    // MARK: - UI (все токены — KrabEarTheme)

    private let recordIndicator = RecordingIndicator()
    private let headerTimerLabel = NSTextField(labelWithString: "00:00")
    private let statusLabel = NSTextField(labelWithString: "")
    private let liveTextView = NSTextView()
    private let liveTextContainer = NSView()
    private let notesStack = NSStackView()
    private let toggleButton = ThemePrimaryButton(title: "", target: nil, action: nil)
    private let copyAllButton = ThemeSecondaryButton(title: "Копировать всё", target: nil, action: nil)
    private let sendToNotesButton = ThemeSecondaryButton(title: "→ Notes", target: nil, action: nil)

    private static let maxNoteRows = 7

    // MARK: - Test hooks (паттерн MeetingLivePanelController)

    var _testStatusText: String { statusLabel.stringValue }
    var _testHeaderTimerActive: Bool { timerTick != nil }
    var _testLiveText: String { liveTextView.string }
    var _testNoteRowCount: Int { notesStack.arrangedSubviews.count }

    func _testSetRecording(_ recording: Bool) { setRecording(recording) }
    func _testIngestPartial(_ text: String) { ingestPartial(text) }
    func _testSetNotes(_ notes: [[String: Any]]) { renderNotes(notes) }

    // MARK: - Init

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 420),
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered, defer: false)
        super.init()
        setupPanel()
        // Панель закрываема крестиком и переиспользуема — isReleasedWhenClosed=false
        // обязателен, иначе повторный show() после закрытия обращается к
        // деаллоцированному окну (тот же урок, что MeetingLivePanelController).
        panel.styleMask.insert(.closable)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        buildLayout()
        renderNotes([])
        applyStatusText()
    }

    // MARK: - Public API

    /// Показывает панель на сохранённой (или дефолтной топ-правой) позиции.
    /// nonactivatingPanel — НЕ активирует приложение, фокус остаётся у текущего.
    func show() {
        restorePosition()
        panel.orderFront(nil)
    }

    func hide() {
        panel.orderOut(nil)
    }

    /// Ревью-урок C2c №4: header-таймер не должен тикать за закрытой панелью.
    /// Сама запись (backend) закрытием панели НЕ останавливается.
    func windowWillClose(_ notification: Notification) {
        stopTimer()
    }

    // MARK: - Recording state (чистая функция состояния → UI)

    /// Единственная точка входа состояния записи — владелец (Task 2) зовёт её
    /// из внешнего источника (poll/событие quickCaptureActive), кнопка ⏺/■
    /// сама НЕ меняет это состояние напрямую — она только форвардит намерение
    /// пользователя через onToggleRecording (см. onToggleTapped).
    func setRecording(_ recording: Bool) {
        guard recording != isRecording else { return }
        isRecording = recording
        if recording {
            startedAt = Date().timeIntervalSince1970
            liveTextView.string = ""
            recordIndicator.startPulsing()
            startTimer()
        } else {
            recordIndicator.stopPulsing()
            stopTimer()
        }
        applyStatusText()
        updateToggleButton()
    }

    private func applyStatusText() {
        statusLabel.stringValue = isRecording ? "Идёт запись…" : "Запись не идёт. Готов начать."
    }

    private func updateToggleButton() {
        let symbolName = isRecording ? "stop.circle.fill" : "record.circle.fill"
        let accessibilityLabel = isRecording ? "Остановить запись" : "Начать запись"
        toggleButton.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: accessibilityLabel)
        toggleButton.imagePosition = .imageLeading
        toggleButton.title = isRecording ? "Остановить" : "Начать запись"
    }

    // MARK: - Live text (партиалы — Task 2 поставляет через SSE/poll)

    /// Копится инкрементально; setRecording(true) сбрасывает буфер под новую
    /// заметку (тот же приём, что transcriptTailLabel в MeetingLivePanelController,
    /// но здесь весь текст, а не хвост — заметки короткие).
    func ingestPartial(_ text: String) {
        guard !text.isEmpty else { return }
        if !liveTextView.string.isEmpty { liveTextView.string += " " }
        liveTextView.string += text
    }

    // MARK: - Notes list (до 7 строк: текст-превью + «Копировать»)

    /// Единственная точка входа списка заметок. `notes` — словари вида
    /// {"text": String, "ts": String} (поля сверены с HistoryItem.to_dict(),
    /// тот же контракт, что rebuildQuickNotesSubmenu в main+QuickCapture.swift).
    func renderNotes(_ notes: [[String: Any]]) {
        notesStack.arrangedSubviews.forEach { notesStack.removeArrangedSubview($0); $0.removeFromSuperview() }
        let latest = notes.prefix(Self.maxNoteRows)
        if latest.isEmpty {
            let emptyLabel = NSTextField(labelWithString: "Заметок пока нет")
            emptyLabel.font = KrabEarTheme.Typography.caption
            emptyLabel.textColor = KrabEarTheme.Colors.textSecondary
            emptyLabel.isBordered = false
            emptyLabel.drawsBackground = false
            notesStack.addArrangedSubview(emptyLabel)
            return
        }
        for note in latest {
            let text = note["text"] as? String ?? ""
            notesStack.addArrangedSubview(makeNoteRow(text: text))
        }
    }

    private func makeNoteRow(text: String) -> NSView {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let previewLimit = 60
        let preview: String
        if trimmed.isEmpty {
            preview = "(без текста)"
        } else if trimmed.count > previewLimit {
            preview = String(trimmed.prefix(previewLimit)) + "…"
        } else {
            preview = trimmed
        }
        return QuickNoteRowView(previewText: preview) { [weak self] in
            self?.copyToClipboard(text)
        }
    }

    private func copyToClipboard(_ text: String) {
        guard !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        BackendToast.shared.show("Скопировано")
    }

    // MARK: - «→ Notes» видимость (Task 2 читает quick_capture_send_to_notes)

    func setSendToNotesVisible(_ visible: Bool) {
        sendToNotesButton.isHidden = !visible
    }

    // MARK: - Button actions

    @objc private func onToggleTapped() {
        onToggleRecording?()
    }

    @objc private func onCopyAllTapped() {
        copyToClipboard(liveTextView.string)
    }

    /// create_apple_note IPC — строго off-main (AGENT-3, CLAUDE.md). Параметры
    /// title/body/folder сверены с sendQuickCaptureNoteToAppleNotes
    /// (main+QuickCapture.swift) — тот же контракт backend'а
    /// (apple_integration_service.py::handle_create_apple_note).
    @objc private func onSendToNotesTapped() {
        let text = liveTextView.string
        guard !text.isEmpty else {
            BackendToast.shared.show("Нечего отправлять — текст пуст")
            return
        }
        guard let client = ipcClient else {
            BackendToast.shared.show("Нет соединения с backend'ом")
            return
        }
        let firstLine = text.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
            .first.map(String.init) ?? text
        let trimmedFirstLine = firstLine.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = trimmedFirstLine.count > 60
            ? String(trimmedFirstLine.prefix(60)) + "…"
            : trimmedFirstLine
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "create_apple_note",
                    params: ["title": title.isEmpty ? "Быстрая заметка" : title,
                             "body": text, "folder": "Krab Ear"])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    if (result["ok"] as? Bool) == true {
                        BackendToast.shared.show("Отправлено в Notes")
                    } else {
                        // Ответ backend'а несёт user_msg (человекочитаемо, напр.
                        // privacy-гейт) либо error (техническое сообщение osascript).
                        let message = (result["user_msg"] as? String)
                            ?? (result["error"] as? String)
                            ?? "Не удалось отправить в Notes"
                        BackendToast.shared.show(message)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Не удалось отправить в Notes: \(error.localizedDescription)")
                }
            }
        }
    }

    // MARK: - Header-таймер (mm:ss от startedAt, только пока isRecording)

    private func startTimer() {
        stopTimer()
        updateTimer()
        timerTick = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updateTimer() }
        }
    }

    private func stopTimer() {
        timerTick?.invalidate()
        timerTick = nil
    }

    private func updateTimer() {
        guard isRecording, let startedAt else { return }
        let elapsed = max(0, Int(Date().timeIntervalSince1970 - startedAt))
        headerTimerLabel.stringValue = String(format: "%02d:%02d", elapsed / 60, elapsed % 60)
    }

    // MARK: - Panel setup (паттерн MeetingLivePanelController/ConversationStatusOverlay)

    private func setupPanel() {
        panel.styleMask.insert(.resizable)
        panel.minSize = NSSize(width: 300, height: 320)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
        panel.alphaValue = 0.97
    }

    private func buildLayout() {
        headerTimerLabel.font = KrabEarTheme.Typography.display.tabular()
        headerTimerLabel.textColor = KrabEarTheme.Colors.textPrimary
        headerTimerLabel.isBordered = false
        headerTimerLabel.drawsBackground = false

        statusLabel.font = KrabEarTheme.Typography.body
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.isBordered = false
        statusLabel.drawsBackground = false
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let headerRow = NSStackView(views: [recordIndicator, headerTimerLabel, statusLabel])
        headerRow.orientation = .horizontal
        headerRow.spacing = KrabEarTheme.Metrics.standard
        headerRow.alignment = .centerY

        liveTextView.isEditable = false
        liveTextView.isSelectable = true
        liveTextView.font = KrabEarTheme.Typography.body
        liveTextView.textColor = KrabEarTheme.Colors.textPrimary
        liveTextView.backgroundColor = .clear
        liveTextView.drawsBackground = false
        liveTextView.string = ""

        let liveScroll = NSScrollView()
        liveScroll.hasVerticalScroller = true
        liveScroll.drawsBackground = false
        liveScroll.documentView = liveTextView
        liveScroll.translatesAutoresizingMaskIntoConstraints = false
        liveScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true

        // Карточка-обрамление под live-текст — тот же приём, что
        // transcriptContainer в MeetingLivePanelController.
        liveTextContainer.wantsLayer = true
        liveTextContainer.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        liveTextContainer.layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        liveTextContainer.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        liveTextContainer.layer?.borderWidth = 1.0
        liveTextContainer.translatesAutoresizingMaskIntoConstraints = false
        liveTextContainer.addSubview(liveScroll)
        NSLayoutConstraint.activate([
            liveScroll.topAnchor.constraint(equalTo: liveTextContainer.topAnchor, constant: KrabEarTheme.Metrics.tight),
            liveScroll.bottomAnchor.constraint(equalTo: liveTextContainer.bottomAnchor, constant: -KrabEarTheme.Metrics.tight),
            liveScroll.leadingAnchor.constraint(equalTo: liveTextContainer.leadingAnchor, constant: KrabEarTheme.Metrics.tight),
            liveScroll.trailingAnchor.constraint(equalTo: liveTextContainer.trailingAnchor, constant: -KrabEarTheme.Metrics.tight),
        ])

        notesStack.orientation = .vertical
        notesStack.spacing = KrabEarTheme.Metrics.tight
        notesStack.alignment = .leading
        notesStack.translatesAutoresizingMaskIntoConstraints = false

        let notesScroll = NSScrollView()
        notesScroll.hasVerticalScroller = true
        notesScroll.drawsBackground = false
        notesScroll.documentView = notesStack
        notesScroll.translatesAutoresizingMaskIntoConstraints = false
        notesScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true

        toggleButton.target = self
        toggleButton.action = #selector(onToggleTapped)
        updateToggleButton()

        copyAllButton.target = self
        copyAllButton.action = #selector(onCopyAllTapped)

        sendToNotesButton.target = self
        sendToNotesButton.action = #selector(onSendToNotesTapped)
        // Видимость управляется владельцем через setSendToNotesVisible(_:) —
        // по умолчанию скрыта до того, как Task 2 прочитает
        // quick_capture_send_to_notes из настроек.
        sendToNotesButton.isHidden = true

        let buttonsRow = NSStackView(views: [toggleButton, copyAllButton, sendToNotesButton])
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.standard
        buttonsRow.alignment = .centerY

        let contentStack = NSStackView(views: [
            headerRow, liveTextContainer, notesScroll, buttonsRow,
        ])
        contentStack.orientation = .vertical
        contentStack.spacing = KrabEarTheme.Metrics.comfortable
        contentStack.alignment = .leading
        contentStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.spacious,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.spacious,
            right: KrabEarTheme.Metrics.spacious
        )
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        let backdrop = NSVisualEffectView()
        backdrop.material = .popover
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.wantsLayer = true
        backdrop.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        backdrop.layer?.cornerCurve = .continuous
        backdrop.layer?.masksToBounds = true

        backdrop.addSubview(contentStack)

        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: backdrop.topAnchor),
            contentStack.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            contentStack.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            contentStack.bottomAnchor.constraint(lessThanOrEqualTo: backdrop.bottomAnchor),
            liveTextContainer.widthAnchor.constraint(equalTo: contentStack.widthAnchor, constant: -(KrabEarTheme.Metrics.spacious * 2)),
            notesScroll.widthAnchor.constraint(equalTo: contentStack.widthAnchor, constant: -(KrabEarTheme.Metrics.spacious * 2)),
            notesStack.widthAnchor.constraint(equalTo: notesScroll.widthAnchor),
        ])

        backdrop.frame = panel.contentView!.bounds
        backdrop.autoresizingMask = [.width, .height]
        panel.contentView = backdrop

        let drag = NSPanGestureRecognizer(target: self, action: #selector(handleDrag(_:)))
        backdrop.addGestureRecognizer(drag)
    }

    // MARK: - Position persistence (паттерн MeetingLivePanelController/ConversationStatusOverlay)

    private func placeTopRight() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        let x = vf.maxX - size.width - 24
        let y = vf.maxY - size.height - 24
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }

    /// Валидна ли candidate-позиция — хотя бы 80% площади пересекается с
    /// visibleFrame какого-нибудь ТЕКУЩЕГО экрана (без этого отключение
    /// второго монитора навсегда прячет панель за экраном).
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
        placeTopRight()
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
}

/// Строка списка заметок: текст-превью + кнопка «Копировать». Отдельный
/// маленький NSView (паттерн SpeakerChipView в MeetingLivePanelController) —
/// хранит колбэк копирования, а не полагается на representedObject кнопки.
private final class QuickNoteRowView: NSView {
    private let onCopy: () -> Void

    init(previewText: String, onCopy: @escaping () -> Void) {
        self.onCopy = onCopy
        super.init(frame: .zero)

        let label = NSTextField(labelWithString: previewText)
        label.font = KrabEarTheme.Typography.body
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.isBordered = false
        label.drawsBackground = false
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 1
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let copyButton = ThemeSecondaryButton(title: "Копировать", target: self, action: #selector(handleCopyTap))
        copyButton.controlSize = .small
        copyButton.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [label, copyButton])
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.standard
        row.alignment = .centerY
        row.translatesAutoresizingMaskIntoConstraints = false

        addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: topAnchor),
            row.bottomAnchor.constraint(equalTo: bottomAnchor),
            row.leadingAnchor.constraint(equalTo: leadingAnchor),
            row.trailingAnchor.constraint(equalTo: trailingAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError() }

    @objc private func handleCopyTap() { onCopy() }
}
