/*
 Запуск и мониторинг Python backend для Krab Ear Agent.

 Связи модуля:
 1) IPCClient: проверка готовности через ping.
 2) main.swift: управление жизненным циклом backend.
*/

import Foundation

/// Управляет жизненным циклом Python backend-процесса.
final class BackendSupervisor {
    private(set) var backendProcess: Process?
    let projectRoot: String
    let dataDir: String
    let socketPath: String

    init(projectRoot: String) {
        self.projectRoot = projectRoot
        self.dataDir = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath
        self.socketPath = (self.dataDir as NSString).appendingPathComponent("krabear.sock")
    }

    func ensureBackendRunning() throws {
        let client = IPCClient(socketPath: socketPath)
        if (try? client.call(method: "ping")) != nil {
            return
        }

        try startBackendProcess()

        // Дожидаемся появления сокета и ответа ping.
        for _ in 0..<30 {
            usleep(200_000)
            if (try? client.call(method: "ping")) != nil {
                return
            }
        }

        throw IPCError.socketConnectFailed("backend не ответил после запуска")
    }

    func stopBackend() {
        backendProcess?.terminate()
        backendProcess = nil
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
