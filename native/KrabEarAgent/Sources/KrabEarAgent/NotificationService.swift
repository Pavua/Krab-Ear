/*
 Локальные уведомления Krab Ear через osascript.

 Почему так:
 1) Агент запускается как standalone-бинарник без .app bundle.
 2) UNUserNotificationCenter в таком режиме может аварийно завершать процесс.
*/

import Foundation

/// Сервис уведомлений без зависимости от app-bundle.
final class NotificationService: @unchecked Sendable {
    func requestAuthorizationIfNeeded() {
        // Для osascript-разрешения не требуются.
    }

    func notify(title: String, body: String) {
        let safeTitle = title.replacingOccurrences(of: "\"", with: "\\\"")
        let safeBody = body.replacingOccurrences(of: "\"", with: "\\\"")

        let script = "display notification \"\(safeBody)\" with title \"\(safeTitle)\""
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        do {
            try process.run()
        } catch {
            // Уведомление не критично, молча пропускаем.
        }
    }
}
