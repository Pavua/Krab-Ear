/*
 Локальные уведомления Krab Ear через osascript.

 Почему так:
 1) Агент запускается как standalone-бинарник без .app bundle.
 2) UNUserNotificationCenter в таком режиме может аварийно завершать процесс.
*/

import Foundation

/// Контракт запуска внешнего процесса для отправки уведомления.
///
/// Выделен отдельно, чтобы unit-тесты проверяли сформированную команду, но никогда
/// не запускали реальный `osascript` и не показывали системные уведомления.
protocol NotificationProcessRunning: Sendable {
    func run(executableURL: URL, arguments: [String]) throws
}

/// Системная реализация запускает процесс без изменения прежнего поведения приложения.
struct SystemNotificationProcessRunner: NotificationProcessRunning {
    func run(executableURL: URL, arguments: [String]) throws {
        let process = Process()
        process.executableURL = executableURL
        process.arguments = arguments
        try process.run()
    }
}

/// Сервис уведомлений без зависимости от app-bundle.
final class NotificationService: @unchecked Sendable {
    private let processRunner: any NotificationProcessRunning

    /// По умолчанию использует системный runner, а тесты передают изолированную реализацию.
    init(processRunner: any NotificationProcessRunning = SystemNotificationProcessRunner()) {
        self.processRunner = processRunner
    }

    func requestAuthorizationIfNeeded() {
        // Для osascript-разрешения не требуются.
    }

    /// Аргументы `osascript`: исходник скрипта СТАТИЧЕН, текст уведомления идёт отдельными
    /// argv-аргументами (`item 1/2 of argv`). Раньше текст склеивался в исходник с
    /// экранированием одних кавычек: `\"` закрывало строку и давало `do shell script`
    /// (в уведомления попадают превью речи, статусы и пути). `--` нужен, чтобы текст,
    /// начинающийся с `-`, не читался как опция osascript.
    static func scriptArguments(title: String, body: String) -> [String] {
        [
            "-e", "on run argv",
            "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e", "end run",
            "--", body, title,
        ]
    }

    func notify(title: String, body: String) {
        try? processRunner.run(
            executableURL: URL(fileURLWithPath: "/usr/bin/osascript"),
            arguments: Self.scriptArguments(title: title, body: body)
        )
    }
}
