/*
 Извлечение задач/решений/вопросов из транскриптов через LLM (PR #289 — backend готов).

 Backend IPC:
   - extract_action_items {id, language?} → {id, ok, action_items: [{text, assignee, due, priority}],
                                              decisions: [...], questions: [...],
                                              fallback_reason?, latency_ms?}

 UI: CollapsibleSection «Действия и решения» в History tab. Кнопка «Извлечь из
 выбранной записи» → IPC → отображение в трёх колонках (Задачи / Решения / Вопросы).
*/

import AppKit
import ObjectiveC

private enum ActionItemsAssoc {
    nonisolated(unsafe) static var statusLabel: UInt8 = 0
    nonisolated(unsafe) static var resultsView: UInt8 = 0
    nonisolated(unsafe) static var languageSelector: UInt8 = 0
}

@MainActor
extension HistoryPanelController {

    // MARK: - Lazy UI elements

    private var actionItemsStatusLabel: NSTextField {
        if let v = objc_getAssociatedObject(self, &ActionItemsAssoc.statusLabel) as? NSTextField {
            return v
        }
        let v = NSTextField(labelWithString: "Выделите запись и нажмите «Извлечь».")
        v.font = KrabEarTheme.Typography.caption
        v.textColor = NSColor.secondaryLabelColor
        objc_setAssociatedObject(self, &ActionItemsAssoc.statusLabel, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    private var actionItemsResultsView: NSTextView {
        if let v = objc_getAssociatedObject(self, &ActionItemsAssoc.resultsView) as? NSTextView {
            return v
        }
        let v = NSTextView()
        v.isEditable = false
        v.isSelectable = true
        v.font = KrabEarTheme.Typography.body
        v.textContainerInset = NSSize(width: 8, height: 8)
        v.minSize = NSSize(width: 0, height: 160)
        objc_setAssociatedObject(self, &ActionItemsAssoc.resultsView, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    private var actionItemsLanguageSelector: NSPopUpButton {
        if let v = objc_getAssociatedObject(self, &ActionItemsAssoc.languageSelector) as? NSPopUpButton {
            return v
        }
        let v = NSPopUpButton(frame: .zero, pullsDown: false)
        v.addItems(withTitles: ["Авто (ru)", "Русский", "English", "Español"])
        objc_setAssociatedObject(self, &ActionItemsAssoc.languageSelector, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    // MARK: - Section builder

    public func setupActionItemsSection() -> CollapsibleSectionView {
        let card = ThemeCardView()

        let extractButton = ThemePrimaryButton(title: "Извлечь из выбранной записи", target: self, action: #selector(extractActionItemsAction))
        let helpLabel = NSTextField(labelWithString: "LLM (qwen3-4b) ⇒ задачи · решения · вопросы")
        helpLabel.font = KrabEarTheme.Typography.caption
        helpLabel.textColor = NSColor.tertiaryLabelColor

        let actionsRow = NSStackView(views: [extractButton, actionItemsLanguageSelector, helpLabel, NSView()])
        actionsRow.orientation = .horizontal
        actionsRow.spacing = KrabEarTheme.Metrics.standard
        actionsRow.distribution = .fill

        let resultsScroll = NSScrollView()
        resultsScroll.hasVerticalScroller = true
        resultsScroll.borderType = .lineBorder
        resultsScroll.documentView = actionItemsResultsView
        resultsScroll.translatesAutoresizingMaskIntoConstraints = false
        resultsScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true

        card.contentStackView.addArrangedSubview(actionsRow)
        card.contentStackView.addArrangedSubview(actionItemsStatusLabel)
        card.contentStackView.addArrangedSubview(resultsScroll)

        let section = CollapsibleSectionView(
            sectionId: "history_action_items",
            title: "Действия и решения",
            isExpanded: false
        )
        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Action

    @objc func extractActionItemsAction() {
        let selectedRow = self.tableView.selectedRow
        guard selectedRow >= 0, selectedRow < items.count else {
            actionItemsStatusLabel.stringValue = "Сначала выделите запись в таблице."
            return
        }
        let item = items[selectedRow]
        let language: String
        switch actionItemsLanguageSelector.indexOfSelectedItem {
        case 1: language = "ru"
        case 2: language = "en"
        case 3: language = "es"
        default: language = "ru"  // Авто defaulted to ru
        }

        actionItemsStatusLabel.stringValue = "Извлекаю из \(String(item.id.prefix(8)))…  (LLM, может занять до 20 сек)"
        actionItemsResultsView.string = ""

        nonisolated(unsafe) let ipcClient = self.ipcClient
        nonisolated(unsafe) let itemID = item.id
        nonisolated(unsafe) let lang = language

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(
                    method: "extract_action_items",
                    params: ["id": itemID, "language": lang]
                )
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                let formatted = HistoryPanelController.formatActionItemsResult(result: result, itemID: itemID)
                let status = HistoryPanelController.actionItemsStatusText(result: result)
                DispatchQueue.main.async {
                    self?.actionItemsStatusLabel.stringValue = status
                    self?.actionItemsResultsView.string = formatted
                }
            } catch {
                DispatchQueue.main.async {
                    self?.actionItemsStatusLabel.stringValue = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    // MARK: - Pure helpers (testable)

    /// Форматирует результат IPC `extract_action_items` в человекочитаемый текст.
    nonisolated static func formatActionItemsResult(result: [String: Any], itemID: String) -> String {
        let actionItems = result["action_items"] as? [[String: Any]] ?? []
        let decisions = result["decisions"] as? [String] ?? []
        let questions = result["questions"] as? [String] ?? []
        let ok = result["ok"] as? Bool ?? false

        if !ok {
            let reason = result["fallback_reason"] as? String ?? "unknown"
            return "Не удалось извлечь.\nПричина: \(reason)\n(Скорее всего LM Studio offline или модель занята.)"
        }
        if actionItems.isEmpty && decisions.isEmpty && questions.isEmpty {
            return "В транскрипте не найдено действий, решений или вопросов."
        }

        var lines: [String] = []
        lines.append("Запись: \(String(itemID.prefix(8)))…")
        lines.append("")

        if !actionItems.isEmpty {
            lines.append("ЗАДАЧИ (\(actionItems.count))")
            for (i, ai) in actionItems.enumerated() {
                let text = (ai["text"] as? String) ?? ""
                let assignee = (ai["assignee"] as? String) ?? ""
                let due = (ai["due"] as? String) ?? ""
                let priority = (ai["priority"] as? String) ?? "medium"
                let prioMark = priorityMarker(priority)
                var line = "  \(i + 1). \(prioMark) \(text)"
                var meta: [String] = []
                if !assignee.isEmpty { meta.append("@\(assignee)") }
                if !due.isEmpty { meta.append("⏰ \(due)") }
                if !meta.isEmpty { line += "  (\(meta.joined(separator: ", ")))" }
                lines.append(line)
            }
            lines.append("")
        }
        if !decisions.isEmpty {
            lines.append("РЕШЕНИЯ (\(decisions.count))")
            for (i, d) in decisions.enumerated() {
                lines.append("  \(i + 1). ✓ \(d)")
            }
            lines.append("")
        }
        if !questions.isEmpty {
            lines.append("ВОПРОСЫ (\(questions.count))")
            for (i, q) in questions.enumerated() {
                lines.append("  \(i + 1). ? \(q)")
            }
        }
        return lines.joined(separator: "\n")
    }

    /// Краткая статусная строка под кнопкой.
    nonisolated static func actionItemsStatusText(result: [String: Any]) -> String {
        let ok = result["ok"] as? Bool ?? false
        let actionItems = (result["action_items"] as? [[String: Any]])?.count ?? 0
        let decisions = (result["decisions"] as? [String])?.count ?? 0
        let questions = (result["questions"] as? [String])?.count ?? 0
        let latency = result["latency_ms"] as? Int

        if !ok {
            let reason = result["fallback_reason"] as? String ?? "unknown"
            return "Ошибка: \(reason)"
        }
        var parts = ["задач=\(actionItems)", "решений=\(decisions)", "вопросов=\(questions)"]
        if let ms = latency {
            parts.append("\(ms) мс")
        }
        return parts.joined(separator: " · ")
    }

    nonisolated private static func priorityMarker(_ priority: String) -> String {
        switch priority.lowercased() {
        case "high": return "🔴"
        case "low": return "⚪"
        default: return "🟡"
        }
    }
}
