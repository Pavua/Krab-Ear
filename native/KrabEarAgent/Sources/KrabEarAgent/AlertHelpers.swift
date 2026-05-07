/*
 AlertHelpers.swift
 Async-безопасные обёртки над NSAlert, исключающие блокирующий runModal()
 на главном потоке (→ AppHang, Sentry KRAB-EAR-AGENT-E).

 Правило: если есть window → beginSheetModal (неблокирующий).
          если window == nil → логируем и возвращаем nil/false (bail).
          runModal() без window создаёт отдельное modal run loop → NSApp hang.
*/

import AppKit
import Foundation

// MARK: - Информационный алерт (без возвращаемого значения)

/// Показывает информационный алерт как sheet для `window`.
/// Если `window == nil` — тихо логирует сообщение вместо вызова runModal().
@MainActor
func showInfoSheet(window: NSWindow?, title: String, body: String) {
    guard let window else {
        // Без окна runModal() заблокирует main thread → AppHang.
        // Записываем в лог, не показываем модал.
        NSLog("[KrabEar] showInfoSheet bail (no window): %@ — %@", title, body)
        return
    }
    let alert = NSAlert()
    alert.messageText = title
    alert.informativeText = body
    alert.addButton(withTitle: "OK")
    alert.beginSheetModal(for: window, completionHandler: nil)
}

// MARK: - Input alert с текстовым полем

/// Async-версия диалога ввода с NSTextField.
/// Возвращает введённую строку или nil если пользователь нажал "Отмена" / окно недоступно.
@MainActor
func showInputSheet(
    window: NSWindow?,
    title: String,
    message: String,
    placeholder: String = "",
    confirmTitle: String = "OK",
    cancelTitle: String = "Отмена"
) async -> String? {
    guard let window else {
        NSLog("[KrabEar] showInputSheet bail (no window): %@", title)
        return nil
    }
    return await withCheckedContinuation { continuation in
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: confirmTitle)
        alert.addButton(withTitle: cancelTitle)

        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        field.placeholderString = placeholder
        alert.accessoryView = field
        alert.window.initialFirstResponder = field

        alert.beginSheetModal(for: window) { response in
            if response == .alertFirstButtonReturn {
                continuation.resume(returning: field.stringValue)
            } else {
                continuation.resume(returning: nil)
            }
        }
    }
}
