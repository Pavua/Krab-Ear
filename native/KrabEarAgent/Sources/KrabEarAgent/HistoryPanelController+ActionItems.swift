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
        let exportButton = ThemeSecondaryButton(title: "Экспорт всех в .md", target: self, action: #selector(exportAllActionItemsAction))
        exportButton.applyThemeSecondary()
        let helpLabel = NSTextField(labelWithString: "LLM (qwen3-4b) ⇒ задачи · решения · вопросы")
        helpLabel.font = KrabEarTheme.Typography.caption
        helpLabel.textColor = NSColor.tertiaryLabelColor

        let actionsRow = NSStackView(views: [extractButton, exportButton, actionItemsLanguageSelector, helpLabel, NSView()])
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

        let ipcClient = self.ipcClient
        let itemID = item.id
        let lang = language

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

    // MARK: - Markdown export

    /// Экспортирует все existing action_items / decisions / questions из истории в .md.
    /// Загружает страницу истории через `get_history_page` (item-уровневые поля
    /// уже включают `action_items`/`decisions`/`questions` если их извлекали ранее).
    @objc func exportAllActionItemsAction() {
        actionItemsStatusLabel.stringValue = "Загружаю историю…"
        let ipcClient = self.ipcClient

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(
                method: "get_history_page",
                params: ["limit": 500]
            ),
            let result = response["result"] as? [String: Any],
            let rawItems = result["items"] as? [[String: Any]] else {
                DispatchQueue.main.async {
                    self?.actionItemsStatusLabel.stringValue = "Ошибка: не удалось загрузить историю."
                }
                return
            }

            let markdown = HistoryPanelController.formatHistoryItemsAsMarkdown(items: rawItems)
            let withContent = HistoryPanelController.countItemsWithActionContent(items: rawItems)

            DispatchQueue.main.async {
                guard let self = self else { return }
                if withContent == 0 {
                    self.actionItemsStatusLabel.stringValue = "Нет записей с action items / решениями / вопросами. Сначала «Извлечь»."
                    return
                }
                self.actionItemsStatusLabel.stringValue = "Загружено \(rawItems.count) записей; с action data: \(withContent)."

                let panel = NSSavePanel()
                let formatter = DateFormatter()
                formatter.dateFormat = "yyyyMMdd_HHmmss"
                panel.nameFieldStringValue = "krab_action_items_\(formatter.string(from: Date())).md"
                panel.canCreateDirectories = true
                guard panel.runModal() == .OK, let url = panel.url else { return }
                do {
                    try markdown.write(to: url, atomically: true, encoding: .utf8)
                    self.actionItemsStatusLabel.stringValue = "Сохранено: \(url.path)"
                    NSWorkspace.shared.selectFile(url.path, inFileViewerRootedAtPath: "")
                } catch {
                    self.actionItemsStatusLabel.stringValue = "Ошибка записи: \(error.localizedDescription)"
                }
            }
        }
    }

    /// Подсчитывает записи у которых есть хотя бы один action_item / decision / question.
    /// Используется UI'ем чтобы сообщить «нечего экспортировать» до открытия NSSavePanel.
    nonisolated static func countItemsWithActionContent(items: [[String: Any]]) -> Int {
        var n = 0
        for item in items {
            let actionItems = (item["action_items"] as? [[String: Any]]) ?? []
            let decisions = (item["decisions"] as? [String]) ?? []
            let questions = (item["questions"] as? [String]) ?? []
            if !actionItems.isEmpty || !decisions.isEmpty || !questions.isEmpty {
                n += 1
            }
        }
        return n
    }

    /// Формирует Markdown из items (raw IPC response) с action_items / decisions / questions.
    /// Pure helper — `nonisolated`, тестируем без instance.
    nonisolated static func formatHistoryItemsAsMarkdown(items: [[String: Any]]) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"

        var lines: [String] = []
        lines.append("# Krab Ear — Action Items / Decisions / Questions")
        lines.append("")
        lines.append("_Экспортировано: \(formatter.string(from: Date()))_")
        lines.append("")

        var emittedAny = false
        for item in items {
            let actionItems = (item["action_items"] as? [[String: Any]]) ?? []
            let decisions = (item["decisions"] as? [String]) ?? []
            let questions = (item["questions"] as? [String]) ?? []
            if actionItems.isEmpty && decisions.isEmpty && questions.isEmpty {
                continue
            }
            emittedAny = true

            let id = (item["id"] as? String) ?? "?"
            let ts = (item["ts"] as? String) ?? ""
            let textRaw = (item["text"] as? String) ?? ""
            let textSnippet: String = textRaw.count > 200
                ? String(textRaw.prefix(200)) + "…"
                : textRaw

            lines.append("## Запись \(String(id.prefix(8)))…  (\(ts))")
            lines.append("")
            if !textSnippet.isEmpty {
                lines.append("> \(textSnippet.replacingOccurrences(of: "\n", with: " "))")
                lines.append("")
            }

            if !actionItems.isEmpty {
                lines.append("### Задачи (\(actionItems.count))")
                for ai in actionItems {
                    let text = (ai["text"] as? String) ?? ""
                    let assignee = (ai["assignee"] as? String) ?? ""
                    let due = (ai["due"] as? String) ?? ""
                    let priority = (ai["priority"] as? String) ?? "medium"
                    let prio = priorityMarker(priority)
                    var meta: [String] = []
                    if !assignee.isEmpty { meta.append("@\(assignee)") }
                    if !due.isEmpty { meta.append("⏰ \(due)") }
                    let metaSuffix = meta.isEmpty ? "" : "  *(\(meta.joined(separator: ", ")))*"
                    lines.append("- [ ] \(prio) \(text)\(metaSuffix)")
                }
                lines.append("")
            }
            if !decisions.isEmpty {
                lines.append("### Решения (\(decisions.count))")
                for d in decisions {
                    lines.append("- ✓ \(d)")
                }
                lines.append("")
            }
            if !questions.isEmpty {
                lines.append("### Вопросы (\(questions.count))")
                for q in questions {
                    lines.append("- ? \(q)")
                }
                lines.append("")
            }
            lines.append("---")
            lines.append("")
        }

        if !emittedAny {
            lines.append("_(Нет записей с извлечёнными action items.)_")
        }
        return lines.joined(separator: "\n")
    }
}
