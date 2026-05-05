/*
 Расширение HistoryPanelController: секция «Глоссарий переводов» и sheet
 автоматических предложений терминов из истории.

 IPC-методы:
   suggest_medical_glossary_terms(limit: Int)
     → result: [ {source_term, target_term, frequency, domain, confidence} ]
   apply_glossary_suggestions(selected_ids: [Int], suggestions: [[String:Any]])
     → result: {added: Int}

 Добавляет кнопку «Предложить термины из истории» в секцию Перевод
 (Claude Design variant: cdBuildTranslationSection). Sheet содержит
 NSTableView с колонками: ☑ | Исходный термин | Перевод |
 Частота | Домен | Уверенность. Кнопка Apply применяет выбранные строки.
*/

import AppKit
import Foundation

// MARK: - Data model

/// Одна запись из IPC suggest_medical_glossary_terms.
struct GlossarySuggestion {
    let index: Int          // порядковый номер в массиве (используется как id)
    let sourceTerm: String
    let targetTerm: String
    let frequency: Int
    let domain: String
    let confidence: Double

    init(index: Int, dict: [String: Any]) {
        self.index      = index
        self.sourceTerm = (dict["source_term"] as? String) ?? ""
        self.targetTerm = (dict["target_term"] as? String) ?? ""
        self.frequency  = (dict["frequency"]   as? Int)
                       ?? (dict["frequency"]   as? NSNumber)?.intValue ?? 0
        self.domain     = (dict["domain"]      as? String) ?? "general"
        let rawConf     = (dict["confidence"]  as? Double)
                       ?? (dict["confidence"]  as? NSNumber)?.doubleValue ?? 0.0
        self.confidence = rawConf
    }
}

// MARK: - NSWindowController sheet

/// Sheet с таблицей предложенных терминов.
@MainActor
final class GlossarySuggestionsSheetController: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {

    var suggestions: [GlossarySuggestion] = []
    var selectedIndices: Set<Int> = []
    var onApply: (([Int]) -> Void)?

    private let tableView = NSTableView()
    private let scrollView = NSScrollView()
    private let applyButton = NSButton(title: "Применить выбранное", target: nil, action: nil)
    private let cancelButton = NSButton(title: "Отмена", target: nil, action: nil)
    private let statusLabel = NSTextField(labelWithString: "")

    convenience init() {
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 640, height: 420),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        win.title = "Предложения для глоссария"
        win.minSize = NSSize(width: 500, height: 300)
        self.init(window: win)
        buildUI()
    }

    // MARK: Build UI

    private func buildUI() {
        guard let contentView = window?.contentView else { return }
        contentView.wantsLayer = true

        // Table columns
        let checkCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("check"))
        checkCol.title = "☑"
        checkCol.width = 30
        checkCol.minWidth = 30
        checkCol.maxWidth = 30

        let srcCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("source"))
        srcCol.title = "Исходный термин"
        srcCol.width = 150

        let tgtCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("target"))
        tgtCol.title = "Перевод"
        tgtCol.width = 150

        let freqCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("freq"))
        freqCol.title = "Частота"
        freqCol.width = 65

        let domCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("domain"))
        domCol.title = "Домен"
        domCol.width = 90

        let confCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("conf"))
        confCol.title = "Уверенность"
        confCol.width = 90

        for col in [checkCol, srcCol, tgtCol, freqCol, domCol, confCol] {
            tableView.addTableColumn(col)
        }

        tableView.dataSource = self
        tableView.delegate   = self
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.rowHeight = 22
        tableView.gridStyleMask = [.solidHorizontalGridLineMask]

        scrollView.documentView = tableView
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        applyButton.bezelStyle = .rounded
        applyButton.keyEquivalent = "\r"
        applyButton.target = self
        applyButton.action = #selector(didTapApply)
        applyButton.translatesAutoresizingMaskIntoConstraints = false

        cancelButton.bezelStyle = .rounded
        cancelButton.keyEquivalent = "\u{1b}"
        cancelButton.target = self
        cancelButton.action = #selector(didTapCancel)
        cancelButton.translatesAutoresizingMaskIntoConstraints = false

        contentView.addSubview(scrollView)
        contentView.addSubview(statusLabel)
        contentView.addSubview(applyButton)
        contentView.addSubview(cancelButton)

        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 12),
            scrollView.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 12),
            scrollView.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -12),
            scrollView.bottomAnchor.constraint(equalTo: statusLabel.topAnchor, constant: -8),

            statusLabel.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 12),
            statusLabel.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -12),
            statusLabel.trailingAnchor.constraint(equalTo: cancelButton.leadingAnchor, constant: -8),

            cancelButton.trailingAnchor.constraint(equalTo: applyButton.leadingAnchor, constant: -8),
            cancelButton.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -12),

            applyButton.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -12),
            applyButton.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -12),
        ])

        updateStatus()
    }

    // MARK: Load data

    func reload(with items: [GlossarySuggestion]) {
        suggestions = items
        selectedIndices = Set(items.indices) // все выбраны по умолчанию
        tableView.reloadData()
        updateStatus()
    }

    // MARK: Status

    private func updateStatus() {
        let total    = suggestions.count
        let selected = selectedIndices.count
        if total == 0 {
            statusLabel.stringValue = "Нет предложений"
        } else {
            statusLabel.stringValue = "Выбрано \(selected) из \(total)"
        }
    }

    // MARK: NSTableViewDataSource

    func numberOfRows(in tableView: NSTableView) -> Int {
        suggestions.count
    }

    func tableView(
        _ tableView: NSTableView,
        objectValueFor tableColumn: NSTableColumn?,
        row: Int
    ) -> Any? {
        guard row < suggestions.count else { return nil }
        let item = suggestions[row]
        switch tableColumn?.identifier.rawValue {
        case "check":
            return selectedIndices.contains(row) ? NSNumber(value: 1) : NSNumber(value: 0)
        case "source":
            return item.sourceTerm
        case "target":
            return item.targetTerm
        case "freq":
            return "\(item.frequency)"
        case "domain":
            return item.domain
        case "conf":
            return String(format: "%.0f%%", item.confidence * 100)
        default:
            return nil
        }
    }

    func tableView(
        _ tableView: NSTableView,
        setObjectValue object: Any?,
        for tableColumn: NSTableColumn?,
        row: Int
    ) {
        guard tableColumn?.identifier.rawValue == "check", row < suggestions.count else { return }
        let checked = (object as? NSNumber)?.boolValue ?? false
        if checked {
            selectedIndices.insert(row)
        } else {
            selectedIndices.remove(row)
        }
        updateStatus()
    }

    // MARK: NSTableViewDelegate

    func tableView(
        _ tableView: NSTableView,
        dataCellFor tableColumn: NSTableColumn?,
        row: Int
    ) -> NSCell? {
        guard tableColumn?.identifier.rawValue == "check" else { return nil }
        let cell = NSButtonCell()
        cell.setButtonType(.switch)
        cell.controlSize = .small
        cell.title = ""
        cell.imagePosition = .imageOnly
        return cell
    }

    // MARK: Actions

    @objc private func didTapApply() {
        let ids = selectedIndices.sorted()
        onApply?(ids)
        window?.orderOut(nil)
        if let parentWin = window?.sheetParent {
            parentWin.endSheet(window!)
        }
    }

    @objc private func didTapCancel() {
        window?.orderOut(nil)
        if let parentWin = window?.sheetParent {
            parentWin.endSheet(window!)
        }
    }
}

// MARK: - HistoryPanelController extension

extension HistoryPanelController {

    // MARK: - UI builder: кнопка «Предложить термины» для Claude Design

    /// Создаёт кнопку «Предложить термины из истории», которую можно
    /// встроить в секцию Перевод (Claude Design variant).
    @MainActor
    func cdMakeGlossarySuggestButton() -> NSButton {
        let button = NSButton(
            title: "Предложить термины из истории",
            target: self,
            action: #selector(onGlossarySuggestTapped)
        )
        button.bezelStyle = .rounded
        button.font = .systemFont(ofSize: 12)
        button.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        return button
    }

    // MARK: - IPC: fetch suggestions

    @objc func onGlossarySuggestTapped() {
        let client = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let result = try client.call(
                    method: "suggest_medical_glossary_terms",
                    params: ["limit": 20]
                )
                let rawList = (result["result"] as? [[String: Any]])
                           ?? (result["result"] as? [Any])?.compactMap { $0 as? [String: Any] }
                           ?? []
                let suggestions = rawList.enumerated().map { idx, dict in
                    GlossarySuggestion(index: idx, dict: dict)
                }
                DispatchQueue.main.async {
                    self.showGlossarySuggestionsSheet(suggestions: suggestions)
                }
            } catch {
                DispatchQueue.main.async {
                    self.showGlossaryError("Не удалось получить предложения: \(error.localizedDescription)")
                }
            }
        }
    }

    // MARK: - Sheet presentation

    @MainActor
    func showGlossarySuggestionsSheet(suggestions: [GlossarySuggestion]) {
        let sheet = GlossarySuggestionsSheetController()
        sheet.reload(with: suggestions)

        sheet.onApply = { [weak self] selectedIds in
            guard let self else { return }
            self.applyGlossarySuggestions(selectedIds: selectedIds, allSuggestions: suggestions)
        }

        guard let parentWindow = self.window else {
            // fallback: показать как отдельное окно
            sheet.showWindow(nil)
            return
        }
        parentWindow.beginSheet(sheet.window!) { _ in }
        // Держим сильную ссылку пока sheet показан
        objc_setAssociatedObject(self, &AssocKeys.glossarySheet, sheet, .OBJC_ASSOCIATION_RETAIN)
    }

    // MARK: - IPC: apply

    func applyGlossarySuggestions(selectedIds: [Int], allSuggestions: [GlossarySuggestion]) {
        guard !selectedIds.isEmpty else { return }
        // Build typed capture data so closure captures only Sendable types
        let suggestionData: [(sourceTerm: String, targetTerm: String, frequency: Int, domain: String, confidence: Double)] = allSuggestions.map {
            ($0.sourceTerm, $0.targetTerm, $0.frequency, $0.domain, $0.confidence)
        }
        let client = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let rawSuggestions: [[String: Any]] = suggestionData.map { item in
                [
                    "source_term": item.sourceTerm,
                    "target_term": item.targetTerm,
                    "frequency":   item.frequency,
                    "domain":      item.domain,
                    "confidence":  item.confidence,
                ]
            }
            do {
                let result = try client.call(
                    method: "apply_glossary_suggestions",
                    params: [
                        "selected_ids": selectedIds,
                        "suggestions":  rawSuggestions,
                    ]
                )
                let added = (result["result"] as? [String: Any]).flatMap {
                    ($0["added"] as? Int) ?? ($0["added"] as? NSNumber)?.intValue
                } ?? selectedIds.count
                DispatchQueue.main.async {
                    self.showGlossaryToast("Добавлено \(added) терминов в глоссарий")
                }
            } catch {
                DispatchQueue.main.async {
                    self.showGlossaryError("Ошибка применения терминов: \(error.localizedDescription)")
                }
            }
        }
    }

    // MARK: - Toast & error helpers

    @MainActor
    func showGlossaryToast(_ message: String) {
        let alert = NSAlert()
        alert.messageText = message
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        if let w = window {
            alert.beginSheetModal(for: w, completionHandler: nil)
        } else {
            alert.runModal()
        }
    }

    @MainActor
    func showGlossaryError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Ошибка глоссария"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        if let w = window {
            alert.beginSheetModal(for: w, completionHandler: nil)
        } else {
            alert.runModal()
        }
    }
}

// MARK: - Association keys

// nonisolated(unsafe) — значение пишется только из MainActor, читается только
// через objc_setAssociatedObject / objc_getAssociatedObject (Obj-C runtime lock).
private enum AssocKeys {
    nonisolated(unsafe) static var glossarySheet: UInt8 = 0
}
