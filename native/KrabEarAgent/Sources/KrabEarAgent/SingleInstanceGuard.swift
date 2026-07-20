/*
 SingleInstanceGuard — безопасная защита от параллельных экземпляров Krab Ear.

 Основную single-instance гарантию даёт POSIX flock: новый агент завершается,
 если lock уже удерживает работающий экземпляр. Поиск по одному имени процесса
 намеренно не используется, потому что он не отличает production, dev и worktree
 бинарники и способен отправить сигнал не тому процессу.

 Единственная принудительная очистка сохранена для устаревшего бинарника
 `native/runtime/KrabEarAgent`. Кандидаты подтверждаются через `proc_pidpath`,
 канонический путь и start-time процесса дважды проверяются перед SIGKILL.
 Все внешние операции инъецируются, чтобы unit-тесты не отправляли сигналы.

 Файл также содержит очистку теневых bundle из LaunchServices и жизненный цикл
 file lock. Свободные функции оставлены для изолированного тестирования.
*/

import Darwin
import Foundation

// MARK: - Очистка устаревшего runtime-бинарника (Phase C C.6.2)

/// Стабильная identity процесса, достаточная для защиты от повторного использования PID.
struct AgentProcessIdentity: Equatable, Sendable {
    let executablePath: String
    let effectiveUserID: uid_t
    let startSeconds: UInt64
    let startMicroseconds: UInt64
}

/// Возвращает канонический абсолютный путь с разрешёнными символическими ссылками.
private func canonicalExecutablePath(_ path: String) -> String {
    URL(fileURLWithPath: path)
        .standardizedFileURL
        .resolvingSymlinksInPath()
        .path
}

/// Минимальная системная identity, читаемая через `proc_pidinfo`.
private struct KernelProcessIdentity: Equatable {
    let effectiveUserID: uid_t
    let startSeconds: UInt64
    let startMicroseconds: UInt64
}

/// Читает UID и время старта. Неполный ответ ядра считается отсутствием identity.
private func readKernelProcessIdentity(_ pid: pid_t) -> KernelProcessIdentity? {
    var info = proc_bsdinfo()
    let expectedSize = Int32(MemoryLayout<proc_bsdinfo>.size)
    let actualSize = withUnsafeMutablePointer(to: &info) { pointer in
        proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, pointer, expectedSize)
    }
    guard actualSize == expectedSize else { return nil }

    return KernelProcessIdentity(
        effectiveUserID: info.pbi_uid,
        startSeconds: UInt64(info.pbi_start_tvsec),
        startMicroseconds: UInt64(info.pbi_start_tvusec)
    )
}

/// Читает непротиворечивую identity процесса через `proc_pidpath` и `proc_pidinfo`.
/// Системная identity проверяется до и после чтения пути, чтобы не принять новый процесс,
/// которому система успела повторно выдать тот же PID.
let defaultProcessIdentityReader: @Sendable (pid_t) -> AgentProcessIdentity? = { pid in
    guard pid > 1 else { return nil }
    guard let before = readKernelProcessIdentity(pid) else { return nil }

    var buffer = [CChar](repeating: 0, count: Int(MAXPATHLEN * 4))
    let capacity = UInt32(buffer.count)
    let length = buffer.withUnsafeMutableBufferPointer { pointer -> Int32 in
        guard let baseAddress = pointer.baseAddress else { return 0 }
        return proc_pidpath(pid, baseAddress, capacity)
    }
    guard length > 0 else { return nil }
    guard let after = readKernelProcessIdentity(pid), after == before else { return nil }

    return AgentProcessIdentity(
        executablePath: canonicalExecutablePath(String(cString: buffer)),
        effectiveUserID: before.effectiveUserID,
        startSeconds: before.startSeconds,
        startMicroseconds: before.startMicroseconds
    )
}

/// Отправляет системный сигнал. В тестах всегда заменяется безопасным замыканием-регистратором.
let defaultAgentSignalSender: @Sendable (pid_t, Int32) -> Int32 = { pid, signal in
    Darwin.kill(pid, signal)
}

/// Завершает только процессы, чей реальный executable совпадает с
/// `<projectRoot>/native/runtime/KrabEarAgent` после канонизации пути.
/// Identity читается дважды: смена пути, UID или start-time отменяет сигнал.
///
/// - Parameters:
///   - projectRoot: Корень проекта (содержащий `native/runtime/KrabEarAgent`).
///   - logger: Получатель диагностических сообщений; `nil` отключает логирование.
///   - psRunner: Инъецируемый запуск `ps`, возвращающий только PID.
///   - identityReader: Инъецируемое чтение identity процесса.
///   - signalSender: Инъецируемая отправка сигнала.
/// - Returns: Количество процессов, для которых signalSender вернул успех.
@discardableResult
func killOrphanRuntimeProcesses(
    projectRoot: URL,
    logger: AgentLogger? = nil,
    psRunner: (_ arguments: [String]) -> String = defaultPsRunner,
    identityReader: (pid_t) -> AgentProcessIdentity? = defaultProcessIdentityReader,
    signalSender: (pid_t, Int32) -> Int32 = defaultAgentSignalSender
) -> Int {
    let runtimeBinaryPath = canonicalExecutablePath(
        projectRoot.appendingPathComponent("native/runtime/KrabEarAgent").path
    )

    let output = psRunner(["-axo", "pid="])
    guard !output.isEmpty else { return 0 }

    let selfPID = getpid()
    let currentUserID = geteuid()
    let candidatePIDs = Set(output.split(whereSeparator: \Character.isNewline).compactMap { token in
        pid_t(token.trimmingCharacters(in: .whitespaces))
    })
    var killed = 0

    for pid in candidatePIDs.sorted() {
        guard pid > 1, pid != selfPID else { continue }
        guard let firstRead = identityReader(pid) else { continue }
        let firstIdentity = AgentProcessIdentity(
            executablePath: canonicalExecutablePath(firstRead.executablePath),
            effectiveUserID: firstRead.effectiveUserID,
            startSeconds: firstRead.startSeconds,
            startMicroseconds: firstRead.startMicroseconds
        )
        guard firstIdentity.effectiveUserID == currentUserID else { continue }
        guard firstIdentity.executablePath == runtimeBinaryPath else { continue }

        guard let secondRead = identityReader(pid) else { continue }
        let secondIdentity = AgentProcessIdentity(
            executablePath: canonicalExecutablePath(secondRead.executablePath),
            effectiveUserID: secondRead.effectiveUserID,
            startSeconds: secondRead.startSeconds,
            startMicroseconds: secondRead.startMicroseconds
        )
        guard secondIdentity == firstIdentity else {
            logger?.warn("Legacy runtime pid=\(pid) сменил identity перед сигналом — пропускаем")
            continue
        }

        logger?.warn("Завершаем точный legacy runtime pid=\(pid) path=\(runtimeBinaryPath)")
        if signalSender(pid, SIGKILL) == 0 {
            killed += 1
        } else {
            let errorMessage = String(cString: strerror(errno))
            logger?.error("Не удалось завершить legacy runtime pid=\(pid): \(errorMessage)")
        }
    }

    return killed
}

// MARK: - Стандартный запуск ps

/// Запускает `/bin/ps` с переданными аргументами и возвращает stdout.
let defaultPsRunner: @Sendable ([String]) -> String = { arguments in
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/bin/ps")
    task.arguments = arguments
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = Pipe() // stderr намеренно не выводится в startup-лог
    do {
        try task.run()
    } catch {
        return ""
    }
    // Сначала освобождаем pipe и только затем ждём завершения. Иначе большой
    // вывод `ps` способен заполнить буфер и навсегда заблокировать startup-задачу.
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    task.waitUntilExit()
    return String(data: data, encoding: .utf8) ?? ""
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
