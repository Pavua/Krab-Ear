/*
 HistoryPanelController+MeetingMode.swift

 Панель «Встреча» — развёрнутый отчёт по одной записи истории.
 Пункт контекстного меню «Открыть как встречу» (одиночный выбор).

 IPC: get_meeting_report { id } → result {
     ok, summary, summary_is_llm, action_items, decisions,
     questions, speakers, speaker_count, word_count, ts,
     markdown, fallback_reason
 }

 Паттерны:
 - ipcClient.call — строго off-main (DispatchQueue.global → DispatchQueue.main). AGENT-3.
 - resp["result"] разворачивается первым, затем ключи внутри него.
 - НИКОГДА runModal() — только presentPanelSheet / hostWindow.beginSheet. AppHang guard.
 - KrabEarTheme токены — ноль хардкодных цветов и метрик.
 - Glyph-safe: SF Symbols для иконок, plain RU в NSTextField.
 - Пустые секции ([] / nil) → graceful «—», никогда не крашат.
 - Privacy-mode / not_found → информационный алерт, панель не открывается.
*/

import AppKit
import Foundation
import UniformTypeIdentifiers

// MARK: - MeetingReportViewController

/// Полноценное окно-шит с прокручиваемым отчётом встречи.
@MainActor
final class MeetingReportViewController: NSViewController {

    // MARK: Data

    private let summary: String
    private let summaryIsLLM: Bool
    private let actionItems: [String]
    private let decisions: [String]
    private let questions: [String]
    private let speakers: [[String: Any]]
    private let speakerCount: Int
    private let wordCount: Int
    private let ts: String
    private let markdown: String

    // MARK: Init

    init(
        summary: String,
        summaryIsLLM: Bool,
        actionItems: [String],
        decisions: [String],
        questions: [String],
        speakers: [[String: Any]],
        speakerCount: Int,
        wordCount: Int,
        ts: String,
        markdown: String
    ) {
        self.summary = summary
        self.summaryIsLLM = summaryIsLLM
        self.actionItems = actionItems
        self.decisions = decisions
        self.questions = questions
        self.speakers = speakers
        self.speakerCount = speakerCount
        self.wordCount = wordCount
        self.ts = ts
        self.markdown = markdown
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    // MARK: loadView

    override func loadView() {
        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = KrabEarTheme.Colors.windowBackground.cgColor
        self.view = root

        // Outer vertical stack
        let outerStack = NSStackView()
        outerStack.orientation = .vertical
        outerStack.spacing = KrabEarTheme.Metrics.comfortable
        outerStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.spacious,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.spacious,
            right: KrabEarTheme.Metrics.spacious
        )
        outerStack.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(outerStack)
        NSLayoutConstraint.activate([
            outerStack.topAnchor.constraint(equalTo: root.topAnchor),
            outerStack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            outerStack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            outerStack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        // Title row
        let titleRow = NSStackView()
        titleRow.orientation = .vertical
        titleRow.spacing = KrabEarTheme.Metrics.tight
        titleRow.alignment = .leading
        
        let titleLabel = makeLabel(
            text: "Отчёт встречи",
            font: KrabEarTheme.Typography.display,
            color: KrabEarTheme.Colors.textPrimary
        )
        titleRow.addArrangedSubview(titleLabel)

        // Meta info: timestamp + stats
        let metaText = buildMetaString()
        if !metaText.isEmpty {
            let metaLabel = makeLabel(
                text: metaText,
                font: KrabEarTheme.Typography.caption,
                color: KrabEarTheme.Colors.textSecondary
            )
            titleRow.addArrangedSubview(metaLabel)
        }
        
        outerStack.addArrangedSubview(titleRow)
        
        let topSep = makeSeparator()
        outerStack.addArrangedSubview(topSep)

        // Content stack inside scroll view
        let contentStack = NSStackView()
        contentStack.orientation = .vertical
        contentStack.spacing = KrabEarTheme.Metrics.comfortable
        contentStack.alignment = .leading
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        let contentWidth: CGFloat = 580

        // --- Резюме ---
        if !summary.isEmpty {
            let tag = summaryIsLLM ? "Сгенерировано LLM" : "Авто-резюме"
            let card = makeSectionCard(title: "Резюме", symbolName: "doc.text", badge: tag, width: contentWidth)
            let summaryBody = makeMultilineTextField(text: summary, width: contentWidth - (KrabEarTheme.Metrics.comfortable * 2))
            card.contentStackView.addArrangedSubview(summaryBody)
            contentStack.addArrangedSubview(card)
        }

        // --- Задачи ---
        if !actionItems.isEmpty {
            let card = makeSectionCard(title: "Задачи", symbolName: "checkmark.circle", width: contentWidth)
            card.contentStackView.addArrangedSubview(makeBulletList(items: actionItems, width: contentWidth - (KrabEarTheme.Metrics.comfortable * 2), style: .check))
            contentStack.addArrangedSubview(card)
        }

        // --- Решения ---
        if !decisions.isEmpty {
            let card = makeSectionCard(title: "Решения", symbolName: "lightbulb", width: contentWidth)
            card.contentStackView.addArrangedSubview(makeBulletList(items: decisions, width: contentWidth - (KrabEarTheme.Metrics.comfortable * 2), style: .number))
            contentStack.addArrangedSubview(card)
        }

        // --- Вопросы ---
        if !questions.isEmpty {
            let card = makeSectionCard(title: "Вопросы", symbolName: "questionmark.circle", width: contentWidth)
            card.contentStackView.addArrangedSubview(makeBulletList(items: questions, width: contentWidth - (KrabEarTheme.Metrics.comfortable * 2), style: .bullet))
            contentStack.addArrangedSubview(card)
        }

        // --- Спикеры ---
        if !speakers.isEmpty {
            let card = makeSectionCard(title: "Спикеры", symbolName: "person.2", width: contentWidth)
            card.contentStackView.addArrangedSubview(makeSpeakersView(width: contentWidth - (KrabEarTheme.Metrics.comfortable * 2)))
            contentStack.addArrangedSubview(card)
        }

        // Scroll wrapper
        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = false
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        let clipView = scrollView.contentView
        scrollView.documentView = contentStack

        NSLayoutConstraint.activate([
            contentStack.widthAnchor.constraint(equalTo: clipView.widthAnchor),
        ])

        scrollView.heightAnchor.constraint(equalToConstant: 400).isActive = true
        outerStack.addArrangedSubview(scrollView)

        // Separator before buttons
        let bottomSep = makeSeparator()
        outerStack.addArrangedSubview(bottomSep)

        // Button row
        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.spacing = KrabEarTheme.Metrics.standard
        buttonRow.alignment = .centerY

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        buttonRow.addArrangedSubview(spacer)

        let closeBtn = ThemePrimaryButton(
            title: "Закрыть",
            target: self,
            action: #selector(onClose)
        )
        closeBtn.applyThemeSecondary()
        buttonRow.addArrangedSubview(closeBtn)

        let copyBtn = ThemePrimaryButton(
            title: "Копировать",
            target: self,
            action: #selector(onCopyMarkdown)
        )
        copyBtn.applyThemeSecondary()
        buttonRow.addArrangedSubview(copyBtn)

        let saveBtn = ThemePrimaryButton(
            title: "Сохранить дайджест",
            target: self,
            action: #selector(onSaveDigest)
        )
        buttonRow.addArrangedSubview(saveBtn)

        outerStack.addArrangedSubview(buttonRow)
        
        for view in [titleRow, topSep, scrollView, bottomSep, buttonRow] {
            view.widthAnchor.constraint(equalToConstant: contentWidth).isActive = true
        }
    }

    // MARK: - Button Actions

    @objc private func onCopyMarkdown() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(markdown, forType: .string)
        BackendToast.shared.show("Скопировано")
    }

    @objc private func onSaveDigest() {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.allowedContentTypes = [.plainText]
        panel.title = "Сохранить дайджест встречи"
        panel.prompt = "Сохранить"

        // Build default filename from ts (ISO-ish string → safe chars).
        let safeName = ts
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: " ", with: "_")
            .filter { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
        let suggestedName = safeName.isEmpty ? "встреча.md" : "встреча-\(safeName).md"
        panel.nameFieldStringValue = suggestedName

        presentPanelSheet(panel, for: self.view.window) { [weak self] resp in
            guard resp == .OK, let url = panel.url, let self else { return }
            do {
                try self.markdown.write(to: url, atomically: true, encoding: .utf8)
            } catch {
                let alert = NSAlert()
                alert.messageText = "Сохранить дайджест"
                alert.informativeText = "Не удалось записать файл: \(error.localizedDescription)"
                alert.addButton(withTitle: "OK")
                presentAlertSheet(alert, for: self.view.window) { _ in }
            }
        }
    }

    @objc private func onClose() {
        if let window = view.window, let parent = window.sheetParent {
            parent.endSheet(window)
        } else {
            dismiss(nil)
        }
    }

    // MARK: - View helpers

    private func makeLabel(text: String, font: NSFont, color: NSColor) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = font
        label.textColor = color
        label.isBordered = false
        label.drawsBackground = false
        label.isSelectable = false
        label.lineBreakMode = .byWordWrapping
        label.maximumNumberOfLines = 0
        return label
    }

    private func makeSeparator() -> NSView {
        let line = NSBox()
        line.boxType = .separator
        line.translatesAutoresizingMaskIntoConstraints = false
        return line
    }

    private func makeSectionCard(title: String, symbolName: String, badge: String? = nil, width: CGFloat) -> ThemeCardView {
        let card = ThemeCardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        card.widthAnchor.constraint(equalToConstant: width).isActive = true
        
        let headerRow = NSStackView()
        headerRow.orientation = .horizontal
        headerRow.spacing = KrabEarTheme.Metrics.tight
        headerRow.alignment = .centerY
        
        if let img = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil) {
            let imgView = NSImageView(image: img)
            imgView.contentTintColor = KrabEarTheme.Colors.accent
            imgView.translatesAutoresizingMaskIntoConstraints = false
            imgView.widthAnchor.constraint(equalToConstant: 16).isActive = true
            imgView.heightAnchor.constraint(equalToConstant: 16).isActive = true
            headerRow.addArrangedSubview(imgView)
        }
        
        let lbl = makeLabel(
            text: title,
            font: KrabEarTheme.Typography.sectionTitle,
            color: KrabEarTheme.Colors.textPrimary
        )
        headerRow.addArrangedSubview(lbl)
        
        if let badge = badge {
            let spacer = NSView()
            spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
            headerRow.addArrangedSubview(spacer)
            
            let badgeLabel = makeLabel(
                text: badge,
                font: KrabEarTheme.Typography.captionMedium,
                color: KrabEarTheme.Colors.accent
            )
            headerRow.addArrangedSubview(badgeLabel)
        }
        
        headerRow.translatesAutoresizingMaskIntoConstraints = false
        card.contentStackView.addArrangedSubview(headerRow)
        
        let sep = makeSeparator()
        card.contentStackView.addArrangedSubview(sep)
        
        headerRow.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor).isActive = true
        sep.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor).isActive = true
        
        return card
    }

    private enum ListStyle {
        case check
        case number
        case bullet
    }

    private func makeBulletList(items: [String], width: CGFloat, style: ListStyle = .bullet) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = KrabEarTheme.Metrics.tight
        stack.alignment = .leading

        for (index, item) in items.enumerated() {
            let row = NSStackView()
            row.orientation = .horizontal
            row.spacing = KrabEarTheme.Metrics.tight
            row.alignment = .top
            
            switch style {
            case .check:
                if let img = NSImage(systemSymbolName: "circle", accessibilityDescription: nil) {
                    let imgView = NSImageView(image: img)
                    imgView.contentTintColor = KrabEarTheme.Colors.textSecondary
                    imgView.translatesAutoresizingMaskIntoConstraints = false
                    imgView.widthAnchor.constraint(equalToConstant: 12).isActive = true
                    imgView.heightAnchor.constraint(equalToConstant: 12).isActive = true
                    
                    let box = NSView()
                    box.translatesAutoresizingMaskIntoConstraints = false
                    box.widthAnchor.constraint(equalToConstant: 16).isActive = true
                    box.heightAnchor.constraint(equalToConstant: 16).isActive = true
                    box.addSubview(imgView)
                    NSLayoutConstraint.activate([
                        imgView.centerXAnchor.constraint(equalTo: box.centerXAnchor),
                        imgView.centerYAnchor.constraint(equalTo: box.centerYAnchor)
                    ])
                    row.addArrangedSubview(box)
                }
            case .number:
                let markerLabel = makeLabel(
                    text: "\(index + 1).",
                    font: KrabEarTheme.Typography.body,
                    color: KrabEarTheme.Colors.textSecondary
                )
                markerLabel.alignment = .right
                markerLabel.translatesAutoresizingMaskIntoConstraints = false
                markerLabel.widthAnchor.constraint(equalToConstant: 16).isActive = true
                row.addArrangedSubview(markerLabel)
            case .bullet:
                let markerLabel = makeLabel(
                    text: "•",
                    font: KrabEarTheme.Typography.body,
                    color: KrabEarTheme.Colors.textSecondary
                )
                markerLabel.alignment = .right
                markerLabel.translatesAutoresizingMaskIntoConstraints = false
                markerLabel.widthAnchor.constraint(equalToConstant: 10).isActive = true
                row.addArrangedSubview(markerLabel)
            }

            let text = makeLabel(
                text: item,
                font: KrabEarTheme.Typography.body,
                color: KrabEarTheme.Colors.textPrimary
            )
            text.maximumNumberOfLines = 0
            text.preferredMaxLayoutWidth = width - 24
            row.addArrangedSubview(text)

            stack.addArrangedSubview(row)
        }
        return stack
    }

    private func makeSpeakersView(width: CGFloat) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = KrabEarTheme.Metrics.tight
        stack.alignment = .leading

        for speaker in speakers {
            let label = speaker["label"] as? String ?? "?"
            let turns = speaker["turns"] as? Int ?? 0
            let durSec = speaker["duration_sec"] as? Double ?? 0.0
            let mins = Int(durSec) / 60
            let secs = Int(durSec) % 60
            let durString = mins > 0 ? "\(mins)м \(secs)с" : "\(secs)с"
            
            let text = "\(label) — \(turns) реплик, \(durString)"

            let row = NSStackView()
            row.orientation = .horizontal
            row.spacing = KrabEarTheme.Metrics.tight
            row.alignment = .centerY

            if let img = NSImage(systemSymbolName: "person.fill", accessibilityDescription: nil) {
                let imgView = NSImageView(image: img)
                imgView.contentTintColor = KrabEarTheme.Colors.textSecondary
                imgView.translatesAutoresizingMaskIntoConstraints = false
                imgView.widthAnchor.constraint(equalToConstant: 14).isActive = true
                imgView.heightAnchor.constraint(equalToConstant: 14).isActive = true
                
                let box = NSView()
                box.translatesAutoresizingMaskIntoConstraints = false
                box.widthAnchor.constraint(equalToConstant: 16).isActive = true
                box.heightAnchor.constraint(equalToConstant: 16).isActive = true
                box.addSubview(imgView)
                NSLayoutConstraint.activate([
                    imgView.centerXAnchor.constraint(equalTo: box.centerXAnchor),
                    imgView.centerYAnchor.constraint(equalTo: box.centerYAnchor)
                ])
                row.addArrangedSubview(box)
            }

            let lbl = makeLabel(
                text: text,
                font: KrabEarTheme.Typography.body,
                color: KrabEarTheme.Colors.textPrimary
            )
            row.addArrangedSubview(lbl)
            stack.addArrangedSubview(row)
        }
        return stack
    }

    private func makeMultilineTextField(text: String, width: CGFloat) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = KrabEarTheme.Typography.body
        field.textColor = KrabEarTheme.Colors.textPrimary
        field.isSelectable = true
        field.isBordered = false
        field.drawsBackground = false
        field.maximumNumberOfLines = 0
        field.preferredMaxLayoutWidth = width
        return field
    }

    // MARK: - Meta

    private func buildMetaString() -> String {
        var parts: [String] = []
        if !ts.isEmpty { parts.append(ts) }
        if wordCount > 0 { parts.append("\(wordCount) слов") }
        if speakerCount > 0 { parts.append("\(speakerCount) спикеров") }
        return parts.joined(separator: " · ")
    }
}

// MARK: - HistoryPanelController Extension

@MainActor
extension HistoryPanelController {

    // MARK: - Menu item builder

    /// Возвращает NSMenuItem «Открыть как встречу» для контекстного меню.
    /// Активируется только при одиночном выборе (NSMenuItemValidation).
    func makeMeetingMenuItem() -> NSMenuItem {
        let item = NSMenuItem(
            title: "Открыть как встречу",
            action: #selector(onOpenMeeting),
            keyEquivalent: ""
        )
        item.target = self
        if let icon = NSImage(systemSymbolName: "person.2.fill", accessibilityDescription: nil) {
            item.image = icon
        }
        return item
    }

    // MARK: - Action

    @objc func onOpenMeeting() {
        // Guard: ровно одна строка.
        let selected = tableView.selectedRowIndexes
        guard selected.count == 1, let row = selected.first, row < items.count else {
            showInfoAlert(title: "Встреча", body: "Выберите ровно одну запись.")
            return
        }
        let itemID = items[row].id
        let ipcClient = self.ipcClient

        // AGENT-3: IPC строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "get_meeting_report",
                    params: ["id": itemID]
                )
                // Unwrap envelope first, then keys inside result.
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.presentMeetingReport(result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Встреча",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    // MARK: - Result presentation (must run on main thread)

    private func presentMeetingReport(_ result: [String: Any]) {
        let ok = result["ok"] as? Bool ?? false
        guard ok else {
            let reason = result["fallback_reason"] as? String ?? ""
            let body: String
            if reason == "privacy_mode" {
                body = "Недоступно в режиме приватности."
            } else if reason == "not_found" {
                body = "Запись не найдена."
            } else if reason.isEmpty {
                body = "Не удалось сформировать отчёт встречи."
            } else {
                body = "Не удалось сформировать отчёт: \(reason)"
            }
            showInfoAlert(title: "Встреча", body: body)
            return
        }

        guard let vc = HistoryPanelController.makeMeetingReportVC(from: result) else { return }

        let sheetWindow = NSWindow(contentViewController: vc)
        sheetWindow.styleMask = [.titled, .closable, .resizable]
        sheetWindow.title = "Встреча"
        sheetWindow.setContentSize(NSSize(width: 640, height: 580))

        if let hostWindow = self.window {
            hostWindow.beginSheet(sheetWindow, completionHandler: nil)
        }
    }

    // MARK: - VC factory (C2c: переиспользуется sheet-путём выше и standalone-путём
    // панели встречи, у которой нет host-окна для beginSheet)

    /// Чистое построение VC отчёта из IPC-результата get_meeting_report — без
    /// sheet/window-обвязки. Парсинг полей — точная копия прежнего тела
    /// presentMeetingReport(_:), только вынесена в static. nil при ok=false —
    /// вызывающий код сам решает, как сообщить об ошибке (sheet-путь показывает
    /// alert ДО вызова этого хелпера, см. presentMeetingReport выше).
    static func makeMeetingReportVC(from result: [String: Any]) -> MeetingReportViewController? {
        guard result["ok"] as? Bool ?? false else { return nil }

        let summary        = result["summary"]        as? String        ?? ""
        let summaryIsLLM   = result["summary_is_llm"] as? Bool          ?? false
        let actionItems    = result["action_items"]   as? [String]      ?? []
        let decisions      = result["decisions"]      as? [String]      ?? []
        let questions      = result["questions"]      as? [String]      ?? []
        let speakers       = result["speakers"]       as? [[String: Any]] ?? []
        let speakerCount   = result["speaker_count"]  as? Int           ?? 0
        let wordCount      = result["word_count"]     as? Int           ?? 0
        let ts             = result["ts"]             as? String        ?? ""
        let markdown       = result["markdown"]       as? String        ?? ""

        return MeetingReportViewController(
            summary: summary,
            summaryIsLLM: summaryIsLLM,
            actionItems: actionItems,
            decisions: decisions,
            questions: questions,
            speakers: speakers,
            speakerCount: speakerCount,
            wordCount: wordCount,
            ts: ts,
            markdown: markdown
        )
    }

    // MARK: - Standalone presentation (C2c: панель встречи — не NSWindowController-хост)

    /// C2c: отчёт встречи в отдельном titled-окне (панель — не NSWindowController-хост,
    /// поэтому beginSheet недоступен как для sheet-пути onOpenMeeting).
    @MainActor
    static func presentMeetingReportStandalone(result: [String: Any]) {
        guard let vc = makeMeetingReportVC(from: result) else { return }
        let window = NSWindow(contentViewController: vc)
        window.styleMask = [.titled, .closable, .resizable]
        window.title = "Встреча"
        window.setContentSize(NSSize(width: 640, height: 580))
        window.center()
        window.makeKeyAndOrderFront(nil)
        // держим ссылку до закрытия (иначе ARC закроет окно немедленно)
        _standaloneReportWindows.append(window)
        NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: window, queue: .main
        ) { note in
            MainActor.assumeIsolated {
                _standaloneReportWindows.removeAll { $0 === note.object as? NSWindow }
            }
        }
    }
    @MainActor private static var _standaloneReportWindows: [NSWindow] = []
}

// NOTE: NSMenuItemValidation conformance lives in +QuickActions.swift.
// #selector(onOpenMeeting) is included in the singleSelectionSelectors list there.
