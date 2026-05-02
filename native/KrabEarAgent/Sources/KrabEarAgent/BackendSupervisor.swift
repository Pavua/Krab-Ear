/*
 Запуск и мониторинг Python backend для Krab Ear Agent.

 Связи модуля:
 1) IPCClient: проверка готовности через ping.
 2) main.swift: управление жизненным циклом backend.
*/

import Foundation

/// Режим супервизии backend'а.
enum SupervisionMode {
    /// Swift сам управляет lifecycle: спавнит child, чистит socket, respawn'ит.
    /// Используется для standalone developer flow без установленного Variant B.
    case active

    /// Variant B `ai.krab.ear.backend` bootstrapped в launchd. Swift только
    /// пингует и ждёт launchd'ского respawn'а. Socket и process не трогает.
    case passive
}

/// Управляет жизненным циклом Python backend-процесса.
///
/// Поддерживает автоматический перезапуск при обнаружении мёртвого backend
/// (например, после macOS Jetsam SIGKILL из-за нехватки памяти).
final class BackendSupervisor: @unchecked Sendable {
    private(set) var backendProcess: Process?
    let projectRoot: String
    let dataDir: String
    let socketPath: String

    /// Счётчик последовательных перезапусков (сбрасывается при успешном ping).
    private var consecutiveRestarts = 0

    // Circuit breaker state
    private var circuitOpenedAt: Date?
    private static let circuitOpenAfter = 5
    private static let circuitCooldownDefault: TimeInterval = 300  // 5 min

    /// Режим супервизии, определяется один раз при первом обращении (lazy).
    /// Lazy init означает что реальный `launchctl print` вызов происходит
    /// только когда BackendSupervisor начинает работать (обычно в ensureBackendRunning).
    private(set) lazy var supervisionMode: SupervisionMode = Self.detectSupervisionMode()

#if DEBUG
    /// Тест-хук: позволяет форсировать режим без launchctl вызова.
    /// Должен вызываться до первого обращения к supervisionMode.
    func overrideSupervisionMode(_ mode: SupervisionMode) {
        supervisionMode = mode
    }

    /// Тест-хук: инжектируемый ping-предикат. Если задан, используется вместо
    /// реального IPC вызова в isBackendAlive() и ensureBackendRunning().
    var _testPingOverride: (() -> Bool)? = nil

    /// Тест-хук: если задан, ensureBackendRunning() вызывает этот блок вместо
    /// реального spawn/wait (исключает 20-секундный sleep в тестах).
    var _testEnsureOverride: (() throws -> Void)? = nil

    /// Тест-хук: переопределяет cooldown circuit breaker'а (вместо 5 мин).
    var _testCooldownSec: TimeInterval? = nil

    /// Тест-хук: переопределяет задержку backoff (0 = без sleep в тестах).
    var _testBackoffOverride: TimeInterval? = nil
#endif

    init(projectRoot: String) {
        self.projectRoot = projectRoot
        self.dataDir = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath
        self.socketPath = (self.dataDir as NSString).appendingPathComponent("krabear.sock")
    }

    // MARK: — Circuit breaker helpers

    private var circuitCooldown: TimeInterval {
        #if DEBUG
        return _testCooldownSec ?? Self.circuitCooldownDefault
        #else
        return Self.circuitCooldownDefault
        #endif
    }

    /// Backoff schedule: 1st=0s, 2nd=2s, 3rd=5s, 4th+=15s.
    func backoffDelay(attempt: Int) -> TimeInterval {
        switch attempt {
        case ...1: return 0
        case 2: return 2
        case 3: return 5
        default: return 15
        }
    }

    /// True если circuit открыт (5 fails недавно). Восстанавливается через cooldown.
    func isCircuitOpen() -> Bool {
        guard let openedAt = circuitOpenedAt else { return false }
        if Date().timeIntervalSince(openedAt) >= circuitCooldown {
            // cooldown прошёл → закрываем circuit
            circuitOpenedAt = nil
            consecutiveRestarts = 0
            return false
        }
        return true
    }

    /// Определяет, загружен ли Variant B backend plist в launchd.
    ///
    /// Выполняет `launchctl print gui/<uid>/ai.krab.ear.backend`. Exit code 0
    /// означает что label bootstrapped (независимо от текущего running state).
    /// Если launchctl недоступен или запуск упал — fallback на active mode.
    private static func detectSupervisionMode() -> SupervisionMode {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = ["print", "gui/\(getuid())/ai.krab.ear.backend"]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            task.waitUntilExit()
            return task.terminationStatus == 0 ? .passive : .active
        } catch {
            return .active
        }
    }

    /// Проверяет, жив ли backend (процесс запущен + отвечает на ping).
    func isBackendAlive() -> Bool {
#if DEBUG
        if let ping = _testPingOverride { return ping() }
#endif
        let client = IPCClient(socketPath: socketPath)
        switch supervisionMode {
        case .passive:
            // В passive режиме у нас нет своего backendProcess reference.
            // Судим только по ping — launchd-managed backend это всё что важно.
            return (try? client.call(method: "ping")) != nil
        case .active:
            guard let proc = backendProcess, proc.isRunning else { return false }
            return (try? client.call(method: "ping")) != nil
        }
    }

    func ensureBackendRunning() throws {
#if DEBUG
        if let testEnsure = _testEnsureOverride {
            try testEnsure()
            return
        }
#endif
        let client = IPCClient(socketPath: socketPath)

        // Fast path: backend уже отвечает → готов (не важно кто владелец)
        let pingOK: () -> Bool = {
#if DEBUG
            if let ping = self._testPingOverride { return ping() }
#endif
            return (try? client.call(method: "ping")) != nil
        }
        if pingOK() {
            consecutiveRestarts = 0
            return
        }

        // Ping упал. Дальнейшая стратегия зависит от режима супервизии.
        switch supervisionMode {
        case .passive:
            // Variant B launchd management. НЕ трогаем socket файл (может
            // принадлежать живому launchd-managed процессу, который стартует).
            // НЕ спавним свой child (будет race с launchd). Просто ждём пока
            // launchd respawn'ит (KeepAlive=true, ThrottleInterval=5s,
            // Whisper cold start ~5-8s). Максимум 20 секунд.
            for _ in 0..<100 {  // 100 * 200ms = 20s
                usleep(200_000)
                if (try? client.call(method: "ping")) != nil {
                    consecutiveRestarts = 0
                    return
                }
            }
            throw IPCError.socketConnectFailed(
                "backend (launchd Variant B) не отвечает за 20 сек — проверь `launchctl print gui/\(getuid())/ai.krab.ear.backend`"
            )

        case .active:
            // Standalone mode: Swift owns lifecycle, текущая логика сохраняется.
            cleanupStaleSocket()
            try startBackendProcess()
            for _ in 0..<30 {  // 30 * 200ms = 6s — whisper cold start на fresh spawn
                usleep(200_000)
                if (try? client.call(method: "ping")) != nil {
                    consecutiveRestarts = 0
                    return
                }
            }
            throw IPCError.socketConnectFailed("backend не ответил после запуска")
        }
    }

    /// Async версия `ensureBackendRunning()` — НЕ блокирует calling thread.
    /// Polling переписан через `Task.sleep` вместо `usleep` чтобы main thread
    /// не висел в `applicationDidFinishLaunching` (Sentry KRAB-EAR-AGENT-4).
    ///
    /// Caller должен запускать через `Task.detached { try await ... }`,
    /// после успешного завершения переключиться на @MainActor для UI.
    func ensureBackendRunningAsync() async throws {
#if DEBUG
        if let testEnsure = _testEnsureOverride {
            try testEnsure()
            return
        }
#endif
        let client = IPCClient(socketPath: socketPath)

        let pingOK: () async -> Bool = {
#if DEBUG
            if let ping = self._testPingOverride { return ping() }
#endif
            return ((try? await client.callAsync(method: "ping", timeoutSec: IPCClient.quickTimeoutSec)) != nil)
        }

        if await pingOK() {
            consecutiveRestarts = 0
            return
        }

        switch supervisionMode {
        case .passive:
            for _ in 0..<100 {  // 100 * 200ms = 20s
                try? await Task.sleep(nanoseconds: 200_000_000)
                if await pingOK() {
                    consecutiveRestarts = 0
                    return
                }
            }
            throw IPCError.socketConnectFailed(
                "backend (launchd Variant B) не отвечает за 20 сек — проверь `launchctl print gui/\(getuid())/ai.krab.ear.backend`"
            )
        case .active:
            cleanupStaleSocket()
            try startBackendProcess()
            for _ in 0..<30 {  // 30 * 200ms = 6s
                try? await Task.sleep(nanoseconds: 200_000_000)
                if await pingOK() {
                    consecutiveRestarts = 0
                    return
                }
            }
            throw IPCError.socketConnectFailed("backend не ответил после запуска")
        }
    }

    /// Перезапускает backend, если он мёртв. Возвращает true при успешном восстановлении.
    ///
    /// В active режиме: exponential backoff (0/2/5/15s) + circuit breaker после
    /// circuitOpenAfter (5) последовательных неудач. Circuit закрывается через
    /// circuitCooldown (5 мин) автоматически.
    /// В passive режиме полагается на launchd KeepAlive и только ждёт восстановления.
    func restartIfDead() -> Bool {
        if isBackendAlive() {
            consecutiveRestarts = 0
            circuitOpenedAt = nil
            return true
        }

        // Circuit breaker check (только для active mode — passive режим не throttle'им)
        if isCircuitOpen() {
            return false
        }

        switch supervisionMode {
        case .passive:
            // launchd сам respawn'ит. Мы только ждём через ensureBackendRunning.
            // Rate limit не применяем — launchd сам throttle'ит.
            do {
                try ensureBackendRunning()
                return true
            } catch {
                return false
            }

        case .active:
            consecutiveRestarts += 1
            if consecutiveRestarts >= Self.circuitOpenAfter {
                circuitOpenedAt = Date()
                return false
            }

            let delay: TimeInterval
            #if DEBUG
            delay = _testBackoffOverride ?? backoffDelay(attempt: consecutiveRestarts)
            #else
            delay = backoffDelay(attempt: consecutiveRestarts)
            #endif
            if delay > 0 {
                Thread.sleep(forTimeInterval: delay)
            }

            stopBackend()
            do {
                try ensureBackendRunning()
                return true
            } catch {
                return false
            }
        }
    }

    func stopBackend() {
        switch supervisionMode {
        case .passive:
            // launchd владеет процессом. У нас нет child'а для terminate.
            // No-op. Если нужно реально остановить launchd backend — это
            // отдельный user action: `launchctl bootout gui/<uid>/ai.krab.ear.backend`.
            return
        case .active:
            backendProcess?.terminate()
            backendProcess = nil
        }
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
