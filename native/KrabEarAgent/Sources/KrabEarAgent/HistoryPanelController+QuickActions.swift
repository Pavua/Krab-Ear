/*
 HistoryPanelController+QuickActions.swift

 Подменю «Действия с записью» в контекстном меню таблицы истории.
 Четыре действия над ОДНОЙ выбранной записью:
   1. «Резюме»          → summarize_item {id}
   2. «Перевести»       → translate_text {text, ...}
   3. «Отправить в Telegram» → send_to_telegram {text, chat_id}
   4. «Темп речи»       → analyze_speech_pace {text, duration_sec}

 Паттерны:
 - ipcClient.call строго off-main (AGENT-3).
 - Результаты показываем через presentAlertSheet / presentPanelSheet / showInfoAlert
   (никогда runModal — KRAB-EAR-AGENT-E/H AppHang класс).
 - BackendToast.shared.show() для мгновенного non-modal уведомления об успехе.
 - KrabEarTheme токены — никаких хардкодных цветов/метрик.
 - Glyph-safe: SF Symbols для иконок, plain RU текст в NSTextField.
 - Privacy-mode: пустой summary / перевод → graceful «Недоступно в режиме приватности».
*/

import AppKit
import Foundation

extension HistoryPanelController {

    // MARK: - Публичный строитель подменю (вызывается из setupExportSelectionContextMenu)

    /// Создаёт NSMenuItem с подменю «Действия с записью».
    /// Подменю активно только когда выбрана ровно 1 строка.
    func makeQuickActionsMenuItem() -> NSMenuItem {
        let submenuItem = NSMenuItem(
            title: "Действия с записью",
            action: nil,
            keyEquivalent: ""
        )

        let submenu = NSMenu(title: "Действия с записью")

        let summaryItem = NSMenuItem(
            title: "Резюме",
            action: #selector(onQuickSummary),
            keyEquivalent: ""
        )
        summaryItem.target = self
        if let icon = NSImage(systemSymbolName: "text.quote", accessibilityDescription: nil) {
            summaryItem.image = icon
        }
        submenu.addItem(summaryItem)

        let translateItem = NSMenuItem(
            title: "Перевести",
            action: #selector(onQuickTranslate),
            keyEquivalent: ""
        )
        translateItem.target = self
        if let icon = NSImage(systemSymbolName: "character.bubble", accessibilityDescription: nil) {
            translateItem.image = icon
        }
        submenu.addItem(translateItem)

        let telegramItem = NSMenuItem(
            title: "Отправить в Telegram",
            action: #selector(onQuickSendTelegram),
            keyEquivalent: ""
        )
        telegramItem.target = self
        if let icon = NSImage(systemSymbolName: "paperplane", accessibilityDescription: nil) {
            telegramItem.image = icon
        }
        submenu.addItem(telegramItem)

        let paceItem = NSMenuItem(
            title: "Темп речи",
            action: #selector(onQuickSpeechPace),
            keyEquivalent: ""
        )
        paceItem.target = self
        if let icon = NSImage(systemSymbolName: "waveform.path.ecg", accessibilityDescription: nil) {
            paceItem.image = icon
        }
        submenu.addItem(paceItem)

        submenuItem.submenu = submenu
        return submenuItem
    }

    // MARK: - 1. Резюме

    @objc func onQuickSummary() {
        guard let item = selectedSingleItem() else { return }
        let itemID = item.id
        let ipcClient = self.ipcClient

        // AGENT-3: IPC строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "summarize_item",
                    params: ["id": itemID]
                )
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.presentSummaryResult(result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Резюме",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    private func presentSummaryResult(_ result: [String: Any]) {
        // Privacy guard: backend возвращает пустой summary в privacy_mode.
        let summary = result["summary"] as? String ?? ""
        let isLLM = result["llm"] as? Bool ?? false
        let reason = result["reason"] as? String ?? ""

        let body: String
        if summary.isEmpty {
            if reason.contains("privacy") {
                body = "Недоступно в режиме приватности."
            } else {
                body = "Резюме не получено (текст слишком короткий или LLM недоступен)."
            }
        } else {
            let source = isLLM ? "LLM" : "локальный"
            body = "[\(source)]\n\n\(summary)"
        }

        let alert = NSAlert()
        alert.messageText = "Резюме записи"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")

        // Прокручиваемое текстовое поле для длинных резюме.
        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 160))
        textView.string = body
        textView.isEditable = false
        textView.isRichText = false
        textView.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        textView.backgroundColor = .clear
        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 400, height: 160))
        scrollView.documentView = textView
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        alert.accessoryView = scrollView

        presentAlertSheet(alert, for: self.window) { _ in }
    }

    // MARK: - 2. Перевести

    @objc func onQuickTranslate() {
        guard let item = selectedSingleItem() else { return }
        let text = item.text
        guard !text.isEmpty else {
            showInfoAlert(title: "Перевести", body: "Запись не содержит текста.")
            return
        }
        let ipcClient = self.ipcClient

        // AGENT-3: IPC строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "translate_text",
                    params: [
                        "text": text,
                        "translation_mode": "auto",
                        "translation_style": "neutral",
                        "network_mode": "offline_default",
                    ]
                )
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.presentTranslateResult(result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Перевести",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    private func presentTranslateResult(_ result: [String: Any]) {
        // Ключ «text» содержит переведённый текст (см. handle_translate_text).
        let translated = result["text"] as? String ?? ""
        let status = result["status"] as? String ?? ""
        let sourceLang = result["source_lang"] as? String ?? ""
        let targetLang = result["target_lang"] as? String ?? ""

        let body: String
        if translated.isEmpty {
            if status.contains("privacy") || status.contains("offline") {
                body = "Недоступно в режиме приватности."
            } else {
                body = "Перевод не получен (статус: \(status.isEmpty ? "неизвестен" : status))."
            }
        } else {
            let langHint = sourceLang.isEmpty ? "" : "[\(sourceLang) → \(targetLang)]\n\n"
            body = "\(langHint)\(translated)"
        }

        let alert = NSAlert()
        alert.messageText = "Перевод записи"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")

        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 160))
        textView.string = body
        textView.isEditable = false
        textView.isRichText = false
        textView.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        textView.backgroundColor = .clear
        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 400, height: 160))
        scrollView.documentView = textView
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        alert.accessoryView = scrollView

        presentAlertSheet(alert, for: self.window) { _ in }
    }

    // MARK: - 3. Отправить в Telegram

    @objc func onQuickSendTelegram() {
        guard let item = selectedSingleItem() else { return }
        let text = item.text
        guard !text.isEmpty else {
            showInfoAlert(title: "Отправить в Telegram", body: "Запись не содержит текста.")
            return
        }

        let ipcClient = self.ipcClient
        let parentWindow = self.window

        // 1. Загружаем список чатов off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            nonisolated(unsafe) let chatsResponse = try? ipcClient.call(method: "list_telegram_chats", params: [:])
            nonisolated(unsafe) let chatsResult = chatsResponse?["result"] as? [String: Any] ?? [:]
            nonisolated(unsafe) let chats = chatsResult["chats"] as? [[String: Any]] ?? []

            DispatchQueue.main.async {
                self?.presentTelegramSheet(text: text, chats: chats, parentWindow: parentWindow, ipcClient: ipcClient)
            }
        }
    }

    private func presentTelegramSheet(
        text: String,
        chats: [[String: Any]],
        parentWindow: NSWindow?,
        ipcClient: IPCClient
    ) {
        // Строим NSAlert с попапом выбора чата и редактируемым текстом.
        let alert = NSAlert()
        alert.messageText = "Отправить запись в Telegram"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Отправить")
        alert.addButton(withTitle: "Отмена")

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

        let bodyField = NSTextView(frame: NSRect(x: 0, y: 0, width: 380, height: 80))
        bodyField.string = text
        bodyField.isEditable = true
        bodyField.isRichText = false
        bodyField.font = NSFont.systemFont(ofSize: 12)
        let bodyScroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 380, height: 80))
        bodyScroll.documentView = bodyField
        bodyScroll.hasVerticalScroller = true
        bodyScroll.autohidesScrollers = true

        let container = NSStackView(frame: NSRect(x: 0, y: 0, width: 380, height: 115))
        container.orientation = .vertical
        container.spacing = 6
        container.addArrangedSubview(chatPopUp)
        container.addArrangedSubview(bodyScroll)
        container.translatesAutoresizingMaskIntoConstraints = false
        alert.accessoryView = container

        presentAlertSheet(alert, for: parentWindow) { [weak self] resp in
            guard resp == .alertFirstButtonReturn else { return }

            let msgText = bodyField.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !msgText.isEmpty else {
                self?.showInfoAlert(title: "Отправить в Telegram", body: "Текст сообщения не может быть пустым.")
                return
            }

            let selectedChatID: Any = chatPopUp.selectedItem?.representedObject ?? "self"
            let chatTitle = chatPopUp.selectedItem?.title ?? "Telegram"

            // 2. Отправляем off-main.
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                do {
                    nonisolated(unsafe) let response = try ipcClient.call(
                        method: "send_to_telegram",
                        params: ["chat_id": selectedChatID, "text": msgText]
                    )
                    nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                    // Успех: backend возвращает {message_id, sent_at, chat_title}
                    // или {ok: false, error: "...", user_msg_ru: "..."}
                    let okFlag = result["ok"] as? Bool
                    let hasMessageId = result["message_id"] != nil
                    let success = hasMessageId || (okFlag == true)

                    DispatchQueue.main.async {
                        if success {
                            // BackendToast — non-modal, мгновенное подтверждение.
                            BackendToast.shared.show("Отправлено в \(chatTitle)")
                        } else {
                            let userMsg = result["user_msg_ru"] as? String
                                ?? result["error"] as? String
                                ?? "Ошибка отправки"
                            self?.showInfoAlert(title: "Отправить в Telegram", body: userMsg)
                        }
                    }
                } catch {
                    DispatchQueue.main.async {
                        self?.showInfoAlert(
                            title: "Отправить в Telegram",
                            body: "Ошибка IPC: \(error.localizedDescription)"
                        )
                    }
                }
            }
        }
    }

    // MARK: - 4. Темп речи

    @objc func onQuickSpeechPace() {
        guard let item = selectedSingleItem() else { return }
        let text = item.text
        guard !text.isEmpty else {
            showInfoAlert(title: "Темп речи", body: "Запись не содержит текста.")
            return
        }
        // HistoryItem не хранит duration — передаём 0.0.
        // Backend рассчитает WPM/CPM и вернёт duration_sec=0.0;
        // pace_category будет рассчитана по словам при duration=0 → "slow" (защита деления на ноль).
        let durationSec: Double = 0.0
        let ipcClient = self.ipcClient

        // AGENT-3: IPC строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "analyze_speech_pace",
                    params: [
                        "text": text,
                        "duration_sec": durationSec,
                    ]
                )
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.presentSpeechPaceResult(result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Темп речи",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    private func presentSpeechPaceResult(_ result: [String: Any]) {
        // Ключи из PaceReport.as_dict() / _handle_analyze_speech_pace:
        //   words_per_minute, chars_per_minute, pace_category,
        //   estimated_reading_time_sec, word_count, char_count, duration_sec
        if let errMsg = result["error"] as? String {
            showInfoAlert(title: "Темп речи", body: "Ошибка: \(errMsg)")
            return
        }

        let wpm = result["words_per_minute"] as? Double ?? 0.0
        let cpm = result["chars_per_minute"] as? Double ?? 0.0
        let category = result["pace_category"] as? String ?? "—"
        let readingTimeSec = result["estimated_reading_time_sec"] as? Double ?? 0.0
        let wordCount = result["word_count"] as? Int ?? 0
        let durSec = result["duration_sec"] as? Double ?? 0.0

        // Локализация категории.
        let categoryRU: String
        switch category {
        case "slow":      categoryRU = "Медленный"
        case "normal":    categoryRU = "Нормальный"
        case "fast":      categoryRU = "Быстрый"
        case "very_fast": categoryRU = "Очень быстрый"
        default:          categoryRU = category
        }

        let readingMin = Int(readingTimeSec / 60)
        let readingSec = Int(readingTimeSec.truncatingRemainder(dividingBy: 60))
        let readingStr = readingMin > 0
            ? "\(readingMin) мин \(readingSec) сек"
            : "\(readingSec) сек"

        let durMin = Int(durSec / 60)
        let durSecPart = Int(durSec.truncatingRemainder(dividingBy: 60))
        let durStr = durSec > 0
            ? "\(durMin > 0 ? "\(durMin) мин " : "")\(durSecPart) сек"
            : "нет данных"

        let body = """
        Слов в минуту (WPM): \(Int(wpm.rounded()))
        Символов в минуту (CPM): \(Int(cpm.rounded()))
        Категория темпа: \(categoryRU)
        Расчётное время чтения: \(readingStr)
        Слов в записи: \(wordCount)
        Длительность записи: \(durStr)
        """

        let alert = NSAlert()
        alert.messageText = "Темп речи"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")

        let textField = NSTextField(wrappingLabelWithString: body)
        textField.font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        textField.frame = NSRect(x: 0, y: 0, width: 340, height: 130)
        alert.accessoryView = textField

        presentAlertSheet(alert, for: self.window) { _ in }
    }

    // MARK: - Вспомогательные методы

    /// Возвращает единственный выбранный элемент, либо nil с показом уведомления.
    private func selectedSingleItem() -> HistoryItem? {
        let selected = tableView.selectedRowIndexes
        guard selected.count == 1, let row = selected.first, row < items.count else {
            showInfoAlert(title: "Действия с записью", body: "Выберите ровно одну запись.")
            return nil
        }
        return items[row]
    }
}

// MARK: - NSMenuItemValidation (Quick Actions)

extension HistoryPanelController: NSMenuItemValidation {
    /// Активирует пункты быстрых действий только когда выбрана ровно 1 строка.
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        let quickActionSelectors: [Selector] = [
            #selector(onQuickSummary),
            #selector(onQuickTranslate),
            #selector(onQuickSendTelegram),
            #selector(onQuickSpeechPace),
        ]
        guard let action = menuItem.action else { return true }
        if quickActionSelectors.contains(action) {
            return tableView.selectedRowIndexes.count == 1
        }
        // Остальные пункты — стандартное поведение AppKit.
        return true
    }
}
