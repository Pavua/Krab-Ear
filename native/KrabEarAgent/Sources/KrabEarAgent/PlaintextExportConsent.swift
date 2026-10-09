import AppKit

/// Одинаковое явное согласие для ручных файлов и Quick Capture → Obsidian.
/// Без родительского окна consent отсутствует; вложенный modal run loop не нужен.
@MainActor
func presentPlaintextExportConsent(for window: NSWindow?) async -> Bool {
    guard window != nil else { return false }
    let alert = NSAlert()
    alert.messageText = "Разрешить открытые файлы истории?"
    alert.informativeText = "Экспортированные файлы будут содержать открытый текст. Разрешение также действует для Quick Capture → Obsidian до закрытия приложения, смены бэкенда или настроек защиты. Каждая запись проверяется отдельно."
    alert.alertStyle = .warning
    alert.addButton(withTitle: "Разрешить до закрытия приложения")
    alert.addButton(withTitle: "Отмена")
    return await withCheckedContinuation { continuation in
        var replied = false
        presentAlertSheet(alert, for: window) { response in
            guard !replied else { return }
            replied = true
            continuation.resume(returning: response == .alertFirstButtonReturn)
        }
    }
}

@MainActor
extension PlaintextExportCoordinator {
    func prepare(sink: PlaintextExportSink, presenting window: NSWindow?) async throws -> Ticket {
        try await prepare(sink: sink) { await presentPlaintextExportConsent(for: window) }
    }

    func prepareBackend(method: BackendMethod, presenting window: NSWindow?) async throws -> BackendTicket {
        try await prepareBackend(method: method) { await presentPlaintextExportConsent(for: window) }
    }
}
