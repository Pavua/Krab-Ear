/*
 SingleInstanceGuard — defensive guard against duplicate KrabEarAgent processes.

 Проблема: устаревший `native/runtime/KrabEarAgent --launched-by-launchd` может
 остаться как orphan-процесс после перехода на новый .app bundle. В результате
 в Dock появляются два icon, а Settings panel открывается некорректно.

 Решение: при старте ищем все процессы с именем KrabEarAgent кроме текущего
 и убиваем их. Используем pgrep + kill, чтобы поймать и bundle-less бинарники,
 которые не видны через NSRunningApplication.runningApplications(withBundleIdentifier:).

 Функция вынесена как свободная (не метод делегата) для тестируемости.
*/

import Foundation

/// Ищет и убивает все процессы с именем «KrabEarAgent», кроме текущего.
/// Возвращает количество убитых процессов.
///
/// - Parameter pgrepRunner: замена для Process-запуска pgrep (инъекция для тестов).
/// - Returns: Количество убитых дубликатов.
@discardableResult
func killOtherAgentInstances(
    pgrepRunner: (_ arguments: [String]) -> String = defaultPgrepRunner
) -> Int {
    let selfPid = getpid()
    let output = pgrepRunner(["-x", "KrabEarAgent"])
    let pids = output
        .split(separator: "\n")
        .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
        .filter { $0 != selfPid }

    for pid in pids {
        kill(pid, SIGKILL)
    }
    return pids.count
}

// MARK: - Default pgrep runner

/// Запускает `/usr/bin/pgrep` с переданными аргументами и возвращает stdout.
let defaultPgrepRunner: @Sendable ([String]) -> String = { arguments in
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
    task.arguments = arguments
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = Pipe() // silence stderr
    do {
        try task.run()
        task.waitUntilExit()
    } catch {
        return ""
    }
    return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
}
