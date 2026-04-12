import AppKit

extension HistoryPanelController {
    // MARK: - History Enhancement handlers

    @objc func onExportSrt() {
        let selectedRow = tableView.selectedRow
        guard selectedRow >= 0, selectedRow < items.count else {
            notificationService.notify(title: "Krab Ear", body: "Выберите запись для экспорта SRT")
            return
        }
        let item = items[selectedRow]
        guard let response = try? ipcClient.call(method: "export_history_srt", params: ["id": item.id]),
              let result = response["result"] as? [String: Any],
              let path = result["path"] as? String else {
            notificationService.notify(title: "Krab Ear", body: "Ошибка экспорта SRT")
            return
        }
        notificationService.notify(title: "Krab Ear", body: "SRT сохранён")
        NSWorkspace.shared.selectFile(path, inFileViewerRootedAtPath: "")
    }

    @objc func onCleanupHistory() {
        let daysMap = [30, 60, 90, 180, 365]
        let days = daysMap[cleanupDaysSelector.indexOfSelectedItem]
        let alert = NSAlert()
        alert.messageText = "Очистка истории"
        alert.informativeText = "Удалить записи старше \(days) дней? Это действие нельзя отменить."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Удалить")
        alert.addButton(withTitle: "Отмена")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        guard let response = try? ipcClient.call(method: "cleanup_old_history", params: ["days": days]),
              let result = response["result"] as? [String: Any],
              let deleted = result["deleted_count"] as? Int else {
            notificationService.notify(title: "Krab Ear", body: "Ошибка очистки")
            return
        }
        notificationService.notify(title: "Krab Ear", body: "Удалено записей: \(deleted)")
        loadInitial()
    }

    @objc func onVocabSuggestions() {
        guard let response = try? ipcClient.call(method: "get_vocabulary_suggestions", params: [:]),
              let result = response["result"] as? [String: Any],
              let suggestions = result["suggestions"] as? [String] else {
            showDiagnosticsOutput("Нет предложений по словарю")
            return
        }
        var lines: [String] = ["=== Словарь (предложения) ==="]
        for (i, word) in suggestions.enumerated() {
            lines.append("\(i + 1). \(word)")
        }
        showDiagnosticsOutput(lines.joined(separator: "\n"))
    }

    @objc func onGlossarySuggestions() {
        guard let response = try? ipcClient.call(method: "get_glossary_suggestions", params: [:]),
              let result = response["result"] as? [String: Any],
              let suggestions = result["suggestions"] as? [[String: Any]] else {
            showDiagnosticsOutput("Нет предложений по глоссарию")
            return
        }
        var lines: [String] = ["=== Глоссарий (авто-предложения) ==="]
        for (i, item) in suggestions.enumerated() {
            let source = item["source"] as? String ?? "?"
            let target = item["target"] as? String ?? "?"
            let count = item["count"] as? Int ?? 0
            lines.append("\(i + 1). \(source) → \(target) (встречалось: \(count))")
        }
        showDiagnosticsOutput(lines.joined(separator: "\n"))
    }

    // MARK: - get_history_item (double-click detail)

    @objc func onTableViewDoubleClick() {
        let row = tableView.clickedRow
        guard row >= 0, row < items.count else { return }
        let item = items[row]
        guard let response = try? ipcClient.call(method: "get_history_item", params: ["id": item.id]),
              let result = response["result"] as? [String: Any] else {
            showInfoAlert(title: "Запись", body: "Не удалось загрузить детали записи.")
            return
        }
        let text = result["text"] as? String ?? item.text
        let ts = result["ts"] as? String ?? ""
        let wordCount = result["word_count"] as? Int ?? 0
        let transcriptFile = result["transcript_file"] as? String
        var info = """
        \(text)

        --- Метаданные ---
        ID: \(item.id)
        Время: \(ts)
        Слов: \(wordCount)
        """
        if let tf = transcriptFile {
            info += "\nТранскрипт: \(tf)"
        }
        let alert = NSAlert()
        alert.messageText = "Детали записи"
        alert.informativeText = info
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Скопировать")
        alert.addButton(withTitle: "Закрыть")
        let resp = alert.runModal()
        if resp == .alertFirstButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
    }

    // MARK: - summarize_item (single item by ID)

    @objc func onSummarizeItem() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else {
            showDiagnosticsOutput("Выберите запись для summary.")
            return
        }
        let item = items[selected]
        guard let response = try? ipcClient.call(method: "summarize_item", params: ["id": item.id]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось построить summary для записи.")
            return
        }
        let summary = result["summary"] as? String ?? "(нет текста)"
        let isLLM = result["llm"] as? Bool ?? false
        let sourceChars = result["source_chars"] as? Int ?? 0
        let text = """
        === Summary (ID: \(item.id.prefix(8))…) ===
        \(summary)

        [LLM: \(isLLM ? "да" : "нет"), источник: \(sourceChars) символов]
        """
        showDiagnosticsOutput(text)
    }

    func updateHistoryFiltersBadge() {
        var count = 0
        if !currentQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }
        if historyPasteStatusFilter.indexOfSelectedItem > 0 { count += 1 }
        if historyTranslationModeFilter.indexOfSelectedItem > 0 { count += 1 }
        if historyTranslationStatusFilter.indexOfSelectedItem > 0 { count += 1 }
        if !historyFromDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }
        if !historyToDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }

        if count > 0 {
            historyFiltersBadge.stringValue = "Фильтры: \(count)"
            historyFiltersBadge.textColor = .controlAccentColor
            historyFiltersBadge.isHidden = false
        } else {
            historyFiltersBadge.stringValue = "Фильтры: 0"
            historyFiltersBadge.isHidden = true
        }
    }
}
