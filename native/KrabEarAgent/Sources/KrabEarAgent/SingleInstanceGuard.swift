/*
 SingleInstanceGuard — безопасная защита от параллельных экземпляров Krab Ear.

 Основную single-instance гарантию даёт POSIX flock: новый агент завершается,
 если lock уже удерживает работающий экземпляр. Поиск по одному имени процесса
 намеренно не используется, потому что он не отличает production, dev и worktree
 бинарники и способен отправить сигнал не тому процессу.

 Автоматическая принудительная очистка legacy-бинарников отсутствует намеренно:
 macOS не предоставляет атомарный process handle, поэтому между проверкой PID и
 отправкой сигнала PID может быть переиспользован другим процессом. Такой риск
 неприемлем для startup-пути; старые экземпляры обслуживаются только вручную.

 Файл также содержит очистку теневых bundle из LaunchServices и жизненный цикл
 file lock. Свободные функции оставлены для изолированного тестирования.
*/

import Darwin
import Foundation

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
