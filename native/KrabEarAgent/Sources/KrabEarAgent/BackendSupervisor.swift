/*
 Запуск и мониторинг Python backend для Krab Ear Agent.

 Связи модуля:
 1) IPCClient: проверка готовности через ping.
 2) main.swift: управление жизненным циклом backend.
*/

import Foundation

/// Управляет жизненным циклом Python backend-процесса.
///
/// Поддерживает автоматический перезапуск при обнаружении мёртвого backend
/// (например, после macOS Jetsam SIGKILL из-за нехватки памяти).
final class BackendSupervisor {
    private(set) var backendProcess: Process?
    let projectRoot: String
    let dataDir: String
    let socketPath: String

    /// Счётчик последовательных перезапусков (сбрасывается при успешном ping).
    private var consecutiveRestarts = 0
    private static let maxConsecutiveRestarts = 3

    init(projectRoot: String) {
        self.projectRoot = projectRoot
        self.dataDir = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath
        self.socketPath = (self.dataDir as NSString).appendingPathComponent("krabear.sock")
    }

    /// Проверяет, жив ли backend (процесс запущен + отвечает на ping).
    func isBackendAlive() -> Bool {
        guard let proc = backendProcess, proc.isRunning else { return false }
        let client = IPCClient(socketPath: socketPath)
        return (try? client.call(method: "ping")) != nil
    }

    func ensureBackendRunning() throws {
        let client = IPCClient(socketPath: socketPath)
        if (try? client.call(method: "ping")) != nil {
            consecutiveRestarts = 0
            return
        }

        // Если процесс мёртв — чистим stale socket
        cleanupStaleSocket()

        try startBackendProcess()

        // Дожидаемся появления сокета и ответа ping.
        for _ in 0..<30 {
            usleep(200_000)
            if (try? client.call(method: "ping")) != nil {
                consecutiveRestarts = 0
                return
            }
        }

        throw IPCError.socketConnectFailed("backend не ответил после запуска")
    }

    /// Перезапускает backend, если он мёртв. Возвращает true при успешном восстановлении.
    ///
    /// Ограничен `maxConsecutiveRestarts` попытками подряд — при превышении
    /// возвращает false, чтобы не зациклить перезапуски при системном OOM.
    func restartIfDead() -> Bool {
        if isBackendAlive() {
            consecutiveRestarts = 0
            return true
        }

        guard consecutiveRestarts < Self.maxConsecutiveRestarts else {
            return false
        }

        consecutiveRestarts += 1
        stopBackend()

        do {
            try ensureBackendRunning()
            return true
        } catch {
            return false
        }
    }

    func stopBackend() {
        backendProcess?.terminate()
        backendProcess = nil
    }

    /// Удаляет stale Unix socket, оставшийся после убитого процесса.
    private func cleanupStaleSocket() {
        let fm = FileManager.default
        if fm.fileExists(atPath: socketPath) {
            try? fm.removeItem(atPath: socketPath)
        }
    }

    private func startBackendProcess() throws {
        let fileManager = FileManager.default
        try fileManager.createDirectory(
            atPath: dataDir,
            withIntermediateDirectories: true
        )

        let backendScript = (projectRoot as NSString).appendingPathComponent("KrabEar/backend/service.py")
        let venvPython = (projectRoot as NSString).appendingPathComponent(".venv_krab_ear/bin/python")

        let pythonPath: String
        if fileManager.fileExists(atPath: venvPython) {
            pythonPath = venvPython
        } else {
            pythonPath = "/usr/bin/python3"
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [
            backendScript,
            "--data-dir", dataDir,
            "--socket-path", socketPath,
        ]
        process.currentDirectoryURL = URL(fileURLWithPath: projectRoot)

        // Нельзя оставлять нечитабельный Pipe: при переполнении буфера backend может зависнуть.
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        try process.run()
        backendProcess = process
    }
}
