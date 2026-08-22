import AppKit

extension HistoryPanelController {
    // MARK: - History Enhancement handlers
    //
    // Все sync IPC обёрнуты в DispatchQueue.global(qos: .userInitiated) — без этого
    // под нагрузкой backend блокирует main thread → AppHang ≥2000ms (Sentry KRAB-EAR-AGENT-3).
    // NSAlert/NSWorkspace/NSPasteboard остаются на main (требование AppKit).

    @objc func onExportSrt() {
        let selectedRow = tableView.selectedRow
        guard selectedRow >= 0, selectedRow < items.count else {
            notificationService.notify(title: "Krab Ear", body: "Выберите запись для экспорта SRT")
            return
        }
        let item = items[selectedRow]
        let itemID = item.id
        let ipcClient = self.ipcClient
        let notificationService = self.notificationService
        DispatchQueue.global(qos: .userInitiated).async {
            guard let response = try? ipcClient.call(method: "export_history_srt", params: ["id": itemID]),
                  let result = response["result"] as? [String: Any],
                  let path = result["path"] as? String else {
                DispatchQueue.main.async {
                    notificationService.notify(title: "Krab Ear", body: "Ошибка экспорта SRT")
                }
                return
            }
            DispatchQueue.main.async {
                notificationService.notify(title: "Krab Ear", body: "SRT сохранён")
                NSWorkspace.shared.selectFile(path, inFileViewerRootedAtPath: "")
            }
        }
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
        presentAlertSheet(alert, for: self.window) { [weak self] resp in
            guard let self, resp == .alertFirstButtonReturn else { return }
            let ipcClient = self.ipcClient
            let notificationService = self.notificationService
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let response = try? ipcClient.call(method: "cleanup_old_history", params: ["older_than_days": days]),
                      let result = response["result"] as? [String: Any],
                      let deleted = result["deleted_count"] as? Int else {
                    DispatchQueue.main.async {
                        notificationService.notify(title: "Krab Ear", body: "Ошибка очистки")
                    }
                    return
                }
                DispatchQueue.main.async {
                    notificationService.notify(title: "Krab Ear", body: "Удалено записей: \(deleted)")
                    self?.loadInitial()
                }
            }
        }
    }

    @objc func onVocabSuggestions() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_vocabulary_suggestions", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let suggestions = result["suggestions"] as? [[String: Any]] else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Нет предложений по словарю")
                }
                return
            }
            // Форматируем тело на background (Swift 6 Sendable workaround).
            var lines: [String] = ["=== Словарь (предложения) ==="]
            for (i, item) in suggestions.enumerated() {
                let word = item["word"] as? String ?? ""
                let count = item["count"] as? Int ?? 0
                lines.append("\(i + 1). \(word) (\(count))")
            }
            let body = lines.joined(separator: "\n")
            DispatchQueue.main.async {
                self?.showDiagnosticsOutput(body)
            }
        }
    }

    @objc func onGlossarySuggestions() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_glossary_suggestions", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let suggestions = result["suggestions"] as? [[String: Any]] else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Нет предложений по глоссарию")
                }
                return
            }
            // Форматируем тело на background.
            var lines: [String] = ["=== Глоссарий (авто-предложения) ==="]
            for (i, item) in suggestions.enumerated() {
                let source = item["source"] as? String ?? "?"
                let target = item["target"] as? String ?? "?"
                let count = item["count"] as? Int ?? 0
                lines.append("\(i + 1). \(source) → \(target) (встречалось: \(count))")
            }
            let body = lines.joined(separator: "\n")
            DispatchQueue.main.async {
                self?.showDiagnosticsOutput(body)
            }
        }
    }

    // MARK: - get_history_item (double-click detail)

    @objc func onTableViewDoubleClick() {
        let row = tableView.clickedRow
        guard row >= 0, row < items.count else { return }
        let item = items[row]
        let itemID = item.id
        let fallbackText = item.text
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_history_item", params: ["id": itemID]),
                  let result = response["result"] as? [String: Any] else {
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Запись", body: "Не удалось загрузить детали записи.")
                }
                return
            }
            // Извлекаем нужные поля на background (Sendable Strings/Int) перед main.
            let text = result["text"] as? String ?? fallbackText
            let ts = result["ts"] as? String ?? ""
            let wordCount = result["word_count"] as? Int ?? 0
            let transcriptFile = result["transcript_file"] as? String
            var info = """
            \(text)

            --- Метаданные ---
            ID: \(itemID)
            Время: \(ts)
            Слов: \(wordCount)
            """
            if let tf = transcriptFile {
                info += "\nТранскрипт: \(tf)"
            }
            let textForCopy = text
            DispatchQueue.main.async {
                let alert = NSAlert()
                alert.messageText = "Детали записи"
                alert.informativeText = info
                alert.alertStyle = .informational
                alert.addButton(withTitle: "Скопировать")
                alert.addButton(withTitle: "Закрыть")
                presentAlertSheet(alert, for: self?.window) { resp in
                    guard resp == .alertFirstButtonReturn else { return }
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(textForCopy, forType: .string)
                }
            }
        }
    }

    // MARK: - Отправить в Telegram

    @objc func onSendToTelegram() {
        let selectedRow = tableView.selectedRow
        guard selectedRow >= 0, selectedRow < items.count else {
            notificationService.notify(title: "Krab Ear", body: "Выберите запись для отправки в Telegram")
            return
        }
        let item = items[selectedRow]
        let itemText = item.text
        let itemID = item.id

        let ipcClient = self.ipcClient
        let notificationService = self.notificationService
        let parentWindow = self.window  // захват на main для последующего sheet

        // 1. Загрузить список чатов на background thread
        DispatchQueue.global(qos: .userInitiated).async {
            let chats: [[String: Any]]
            if let response = try? ipcClient.call(method: "list_telegram_chats", params: [:]),
               let result = response["result"] as? [String: Any],
               let chatList = result["chats"] as? [[String: Any]] {
                chats = chatList
            } else {
                chats = []
            }

            DispatchQueue.main.async {
                // 2. Показать NSAlert с NSPopUpButton для выбора чата
                let alert = NSAlert()
                alert.messageText = "Отправить транскрипцию в Telegram"
                alert.alertStyle = .informational

                // Поле с текстом записи (редактируемое)
                let bodyField = NSTextView(frame: NSRect(x: 0, y: 0, width: 380, height: 80))
                bodyField.string = itemText
                bodyField.isEditable = true
                bodyField.isRichText = false
                bodyField.font = NSFont.systemFont(ofSize: 12)
                let bodyScroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 380, height: 80))
                bodyScroll.documentView = bodyField
                bodyScroll.hasVerticalScroller = true
                bodyScroll.autohidesScrollers = true

                // Кнопка выбора чата
                let chatPopUp = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 380, height: 25), pullsDown: false)
                if chats.isEmpty {
                    chatPopUp.addItem(withTitle: "Сохранённые сообщения (self)")
                    chatPopUp.lastItem?.representedObject = "self"
                } else {
                    for chat in chats {
                        let title = chat["title"] as? String ?? String(describing: chat["id"] ?? "?")
                        let chatID = chat["id"] ?? "self"
                        chatPopUp.addItem(withTitle: title)
                        chatPopUp.lastItem?.representedObject = chatID
                    }
                }

                // Вертикальный стек: попап + поле текста
                let container = NSStackView(frame: NSRect(x: 0, y: 0, width: 380, height: 120))
                container.orientation = .vertical
                container.spacing = 6
                container.addArrangedSubview(chatPopUp)
                container.addArrangedSubview(bodyScroll)
                container.translatesAutoresizingMaskIntoConstraints = false

                alert.accessoryView = container
                alert.addButton(withTitle: "Отправить")
                alert.addButton(withTitle: "Отмена")

                presentAlertSheet(alert, for: parentWindow) { resp in
                guard resp == .alertFirstButtonReturn else { return }

                let body = bodyField.string.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !body.isEmpty else {
                    notificationService.notify(title: "Krab Ear", body: "Текст сообщения не может быть пустым")
                    return
                }

                let selectedChatID: Any
                if let selected = chatPopUp.selectedItem?.representedObject {
                    selectedChatID = selected
                } else {
                    selectedChatID = "self"
                }
                let chatTitle = chatPopUp.selectedItem?.title ?? "Telegram"

                // 3. Отправить на background thread
                DispatchQueue.global(qos: .userInitiated).async {
                    let params: [String: Any] = [
                        "chat_id": selectedChatID,
                        "text": body,
                    ]
                    if let response = try? ipcClient.call(method: "send_to_telegram", params: params),
                       let result = response["result"] as? [String: Any],
                       result["message_id"] != nil {
                        DispatchQueue.main.async {
                            let successAlert = NSAlert()
                            successAlert.messageText = "Отправлено в \(chatTitle)"
                            successAlert.informativeText = "Запись \(itemID.prefix(8))… успешно отправлена."
                            successAlert.alertStyle = .informational
                            successAlert.addButton(withTitle: "ОК")
                            presentAlertSheet(successAlert, for: parentWindow) { _ in }
                        }
                    } else {
                        // Попробуем извлечь ошибку
                        let errMsg: String
                        if let response = try? ipcClient.call(method: "send_to_telegram", params: params),
                           let errDict = response["error"] as? [String: Any],
                           let msg = errDict["message"] as? String {
                            errMsg = msg
                        } else {
                            errMsg = "Telegram bridge недоступен"
                        }
                        DispatchQueue.main.async {
                            let errAlert = NSAlert()
                            errAlert.messageText = "Ошибка отправки"
                            errAlert.informativeText = errMsg
                            errAlert.alertStyle = .warning
                            errAlert.addButton(withTitle: "ОК")
                            presentAlertSheet(errAlert, for: parentWindow) { _ in }
                        }
                    }
                }
                }  // закрывает completion presentAlertSheet(alert, for: parentWindow)
            }
        }
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
