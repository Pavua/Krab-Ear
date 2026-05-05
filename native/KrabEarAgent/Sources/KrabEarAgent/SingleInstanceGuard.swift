/*
 SingleInstanceGuard — defensive guard against duplicate KrabEarAgent processes.

 Проблема: устаревший `native/runtime/KrabEarAgent --launched-by-launchd` может
 остаться как orphan-процесс после перехода на новый .app bundle. В результате
 в Dock появляются два icon, а Settings panel открывается некорректно.

 Решение: при старте ищем все процессы с именем KrabEarAgent кроме текущего
 и убиваем их. Используем pgrep + kill, чтобы поймать и bundle-less бинарники,
 которые не видны через NSRunningApplication.runningApplications(withBundleIdentifier:).

 Phase C C.6 дополнения:
 - cleanupWorktreeShadows: при старте unregister из LaunchServices все
   "Krab Ear.app" из ".claude/worktrees/agent-XXX/" и re-register основной bundle.
 - acquireFileLock / releaseFileLock: POSIX flock на agent.lock для race-free
   single-instance гарантии (второй процесс сразу terminates).

 Функции вынесены как свободные (не методы делегата) для тестируемости.
*/

import Darwin
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

// MARK: - Orphan runtime binary cleanup (Phase C C.6.2)

/// Ищет и убивает все процессы KrabEarAgent, запущенные из `native/runtime/KrabEarAgent`
/// (legacy dev binary) — не из основного .app bundle.
/// Использует `ps -axo pid,command` + kill(SIGKILL).
/// Idempotent — safe при повторном вызове.
///
/// - Parameters:
///   - projectRoot: Корень проекта (содержащий `native/runtime/KrabEarAgent`).
///   - logger: Используется для warning/error логов; если nil — silent.
///   - psRunner: Замена для Process-запуска ps (инъекция для тестов).
/// - Returns: Количество убитых orphan-процессов.
@discardableResult
func killOrphanRuntimeProcesses(
    projectRoot: URL,
    logger: AgentLogger? = nil,
    psRunner: (_ arguments: [String]) -> String = defaultPsRunner
) -> Int {
    let runtimeBinaryPath = projectRoot
        .appendingPathComponent("native/runtime/KrabEarAgent")
        .path

    let output = psRunner(["-axo", "pid,command"])
    guard !output.isEmpty else { return 0 }

    let myPid = getpid()
    var killed = 0

    for line in output.components(separatedBy: "\n") {
        guard line.contains(runtimeBinaryPath) else { continue }

        // Парсим PID из начала строки (ведущие пробелы + цифры)
        let parts = line.trimmingCharacters(in: .whitespaces)
            .components(separatedBy: " ")
            .filter { !$0.isEmpty }
        guard let firstPart = parts.first, let pid = Int32(firstPart) else { continue }
        guard pid != myPid else { continue }  // никогда не убиваем себя

        logger?.warn("Killing orphan runtime KrabEarAgent pid=\(pid) path=\(runtimeBinaryPath)")
        kill(pid, SIGKILL)
        killed += 1
    }

    return killed
}

// MARK: - Default ps runner

/// Запускает `/bin/ps` с переданными аргументами и возвращает stdout.
let defaultPsRunner: @Sendable ([String]) -> String = { arguments in
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/bin/ps")
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

// MARK: - Worktree shadow cleanup (Phase C C.6)

/// Ищет все `Krab Ear.app` в `.claude/worktrees/agent-*/` под projectRoot,
/// unregister их из LaunchServices и re-register основной bundle.
/// Idempotent — безопасно вызывать при каждом старте.
///
/// - Parameters:
///   - projectRoot: Корень проекта (содержащий `Krab Ear.app` и `.claude/worktrees/`).
///   - logger: Используется для info/warning логов; если nil — silent.
///   - processRunner: Замена для Process-запуска lsregister (инъекция для тестов).
func cleanupWorktreeShadows(
    projectRoot: URL,
    logger: AgentLogger? = nil,
    processRunner: (_ executable: String, _ arguments: [String]) -> Void = defaultProcessRunner
) {
    let lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    let fileManager = FileManager.default

    guard fileManager.fileExists(atPath: lsregister) else {
        logger?.warn("lsregister not found — worktree shadow cleanup skipped")
        return
    }

    let mainBundlePath = projectRoot.appendingPathComponent("Krab Ear.app")
    guard fileManager.fileExists(atPath: mainBundlePath.path) else {
        logger?.warn("Main bundle not found at \(mainBundlePath.path) — cleanup skipped")
        return
    }

    let worktreesPath = projectRoot.appendingPathComponent(".claude/worktrees")
    // Note: do NOT skip hidden files — worktrees live in .claude/ which is a hidden dir.
    guard let enumerator = fileManager.enumerator(
        at: worktreesPath,
        includingPropertiesForKeys: [.isDirectoryKey],
        options: []
    ) else {
        logger?.info("Worktree shadows: .claude/worktrees not found — nothing to cleanup")
        return
    }

    var shadows: [URL] = []
    for case let url as URL in enumerator {
        if url.lastPathComponent == "Krab Ear.app" {
            shadows.append(url)
            enumerator.skipDescendants() // не рекурсировать внутрь .app
        }
    }

    if shadows.isEmpty {
        logger?.info("Worktree shadows: none found")
        return
    }

    logger?.warn("Worktree shadows found: \(shadows.count) — unregistering from LaunchServices")
    for shadow in shadows {
        processRunner(lsregister, ["-u", shadow.path])
        logger?.info("Unregistered shadow: \(shadow.path)")
    }

    // Re-register основного bundle чтобы он остался первым в LaunchServices
    processRunner(lsregister, ["-f", mainBundlePath.path])
    logger?.info("Worktree shadow cleanup complete; main bundle re-registered: \(mainBundlePath.path)")
}

// MARK: - POSIX flock single-instance guard (Phase C C.6)

/// Нитка состояния для file lock — хранится как глобальный синглтон,
/// освобождается при завершении процесса (или явном вызове releaseFileLock).
nonisolated(unsafe) private var _agentLockFD: Int32 = -1

/// Захватывает advisory exclusive POSIX flock на
/// `~/Library/Application Support/KrabEar/agent.lock`.
///
/// - Returns: `true` если lock захвачен (первый экземпляр или lock file недоступен —
///   permissive fallback). `false` если другой экземпляр уже держит lock.
@discardableResult
func acquireFileLock(logger: AgentLogger? = nil) -> Bool {
    let lockDir = (NSString("~/Library/Application Support/KrabEar").expandingTildeInPath)
    let lockPath = (lockDir as NSString).appendingPathComponent("agent.lock")

    // Убеждаемся что директория существует
    try? FileManager.default.createDirectory(
        atPath: lockDir,
        withIntermediateDirectories: true,
        attributes: nil
    )

    let fd = open(lockPath, O_CREAT | O_RDWR, 0o644)
    if fd < 0 {
        let errMsg = String(cString: strerror(errno))
        logger?.error("Failed to open agent.lock: \(errMsg) — permissive fallback (startup continues)")
        return true // permissive: не блокируем старт при недоступности lock file
    }

    let result = flock(fd, LOCK_EX | LOCK_NB)
    if result != 0 {
        close(fd)
        logger?.error("Another Krab Ear instance holds agent.lock — this instance will terminate")
        return false
    }

    _agentLockFD = fd
    logger?.info("File lock acquired: \(lockPath)")
    return true
}

/// Освобождает file lock. Вызывается при штатном завершении агента.
/// Идемпотентен — safe при повторном вызове.
func releaseFileLock(logger: AgentLogger? = nil) {
    let fd = _agentLockFD
    guard fd >= 0 else { return }
    flock(fd, LOCK_UN)
    close(fd)
    _agentLockFD = -1
    logger?.info("File lock released")
}

// MARK: - Default process runner

/// Запускает произвольный исполняемый файл с аргументами; ждёт завершения.
/// Ошибки запуска игнорируются (cleanup non-critical).
let defaultProcessRunner: @Sendable (_ executable: String, _ arguments: [String]) -> Void = { executable, arguments in
    let task = Process()
    task.executableURL = URL(fileURLWithPath: executable)
    task.arguments = arguments
    task.standardOutput = Pipe() // silence stdout
    task.standardError = Pipe()  // silence stderr
    do {
        try task.run()
        task.waitUntilExit()
    } catch {
        // non-critical — silently ignore
    }
}
