// main+QuickReplace.swift — «Заменить слово в последнем тексте» (Cmd+Shift+R)
//
// Быстрое исправление слов без перезаписи — например, GigaAM написал «код»
// вместо «кот». Hotkey открывает NSAlert с двумя полями: старое и новое слово.
// Вызывает IPC replace_word_in_last_transcript, показывает подтверждение.
// После успешной замены исправленный полный текст копируется в буфер обмена
// («Скопировано — ⌘V») — S64 re-paste, спека 2026-07-19.

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Hotkey registration

    /// Регистрирует глобальный монитор клавиатуры для Cmd+Shift+R.
    func startQuickReplaceHotkeyMonitor() {
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return }
            // keyCode 15 = 'r'; проверяем строго Cmd+Shift (без Option/Control)
            let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            guard event.keyCode == 15,
                  mods == [.command, .shift] else { return }
            DispatchQueue.main.async {
                self.onReplaceWordRequested()
            }
        }
    }

    // MARK: - Quick-replace handler

    @objc @MainActor
    func onReplaceWordRequested() {
        let alert = NSAlert()
        alert.messageText = "Заменить слово в последнем тексте"
        alert.informativeText = "Исправьте слово, которое STT распознал неверно."

        // Accessory view: два текстовых поля стопкой
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 6
        stack.frame = NSRect(x: 0, y: 0, width: 300, height: 58)

        let oldField = NSTextField(frame: NSRect(x: 0, y: 30, width: 300, height: 24))
        oldField.placeholderString = "Старое слово (например: код)"

        let newField = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        newField.placeholderString = "Новое слово (например: кот)"

        stack.addArrangedSubview(oldField)
        stack.addArrangedSubview(newField)

        alert.accessoryView = stack
        alert.addButton(withTitle: "Заменить")
        alert.addButton(withTitle: "Отмена")

        // Принудительно передаём фокус в первое поле после появления окна
        DispatchQueue.main.async {
            alert.window.makeFirstResponder(oldField)
        }

        presentAlertSheet(alert, for: NSApp.keyWindow) { [weak self] resp in
            guard let self, resp == .alertFirstButtonReturn else { return }

            let oldWord = oldField.stringValue.trimmingCharacters(in: .whitespaces)
            let newWord = newField.stringValue.trimmingCharacters(in: .whitespaces)
            guard !oldWord.isEmpty, !newWord.isEmpty else {
                self.showReplaceResult(success: false, message: "Оба поля должны быть заполнены.")
                return
            }

            // Off-main IPC (AGENT-3, паттерн Wave 188 из main+Bookmarks.swift):
            // прежний sync callWithRecovery на main из completion алерта на
            // деградированном пути (restartIfDead = полный цикл рестарта
            // backend внутри recovery) блокировал главный поток. IPCClient —
            // Sendable, AgentAppDelegate (@MainActor self) — нет, поэтому
            // локальный `let`. Трейд-офф паттерна (принят в миграциях
            // Bookmarks/HotkeyRecording): без restartIfDead-recovery — при
            // мёртвом backend разовое действие показывает ошибку, самолечение
            // остаётся за BackendSupervisor/HealthMonitor.
            let ipc = self.ipcClient
            Task.detached { [weak self] in
                do {
                    let response = try await ipc.callAsync(
                        method: "replace_word_in_last_transcript",
                        params: ["old_word": oldWord, "new_word": newWord],
                        timeoutSec: 10
                    )
                    let result = response["result"] as? [String: Any] ?? [:]
                    await MainActor.run { [weak self] in
                        self?.handleReplaceWordResponse(result, oldWord: oldWord, newWord: newWord)
                    }
                } catch {
                    await MainActor.run { [weak self] in
                        self?.showReplaceResult(success: false, message: "Ошибка IPC: \(error.localizedDescription)")
                    }
                }
            }
        }
    }

    /// Обработка ответа replace_word_in_last_transcript на main. Вынесена из
    /// completion алерта при переводе IPC off-main (S64 re-paste, спека
    /// 2026-07-19): success/failure-маппинг прежний, добавлено копирование
    /// new_text в буфер.
    @MainActor
    private func handleReplaceWordResponse(_ result: [String: Any], oldWord: String, newWord: String) {
        let ok = result["ok"] as? Bool ?? false
        let count = result["replaced_count"] as? Int ?? 0
        let error = result["error"] as? String

        if ok {
            let autoLearned = result["auto_learned"] as? Bool ?? false
            let noun = count == 1 ? "вхождение" : (count < 5 ? "вхождения" : "вхождений")
            var message = "Заменено \(count) \(noun): «\(oldWord)» → «\(newWord)»."
            // S64 re-paste: исправленный ПОЛНЫЙ текст — в буфер, чтобы
            // пользователь мог сразу вставить его в целевое приложение
            // (выделив там старый текст), без ручного похода в историю.
            // Только success-ветка — буфер пользователя не перезаписывается
            // на ошибках. Privacy: backend в privacy mode отказывает ДО
            // чтения истории, new_text сюда не доходит.
            if let newText = result["new_text"] as? String, !newText.isEmpty {
                pasteService.putToClipboard(newText)
                message += " Скопировано — ⌘V."
            }
            if autoLearned {
                // Closed-loop STT auto-learn (backend/llm_ops_service.py) реально
                // добавил новое слово в stt_hotwords — сообщаем явно, а не молчим.
                message += " Слово «\(newWord)» выучено в словарь STT."
            }
            showReplaceResult(success: true, message: message)
        } else {
            let reason: String
            switch error {
            case "word_not_found":    reason = "Слово «\(oldWord)» не найдено в последней записи."
            case "no_recent_history": reason = "История пуста."
            case "item_not_found":    reason = "Запись не найдена."
            case "missing_words":     reason = "Укажите оба слова."
            default:                  reason = error ?? "Неизвестная ошибка."
            }
            showReplaceResult(success: false, message: reason)
        }
    }

    // MARK: - Result feedback

    @MainActor
    private func showReplaceResult(success: Bool, message: String) {
        if success {
            // Показываем реальный текст результата в menu bar вместо статичного "✓" —
            // тот же паттерн, что и main+Bookmarks.swift::showTemporaryBookmarkMessage,
            // иначе auto-learn ("Слово «X» выучено в словарь STT.") виден только в логе,
            // а не пользователю. Дольше 1.5с у Bookmarks (2.0с на короткий "📌 M:SS"),
            // т.к. это предложение — нужно больше времени, чтобы прочитать.
            if let btn = statusItem?.button {
                let original = btn.title
                btn.title = message
                DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
                    btn.title = original
                }
            }
            logger.info("replace_word: \(message)")
        } else {
            let errAlert = NSAlert()
            errAlert.messageText = "Замена не выполнена"
            errAlert.informativeText = message
            errAlert.alertStyle = .warning
            errAlert.addButton(withTitle: "OK")
            presentAlertSheet(errAlert, for: NSApp.keyWindow) { _ in }
        }
    }
}
