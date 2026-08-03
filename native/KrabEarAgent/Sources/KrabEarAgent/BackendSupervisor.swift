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

/// Честный исход `restartIfDeadDetailed()` — что на самом деле произошло с backend.
///
/// Отдельно от простого `Bool`: вызывающая сторона (тост, лог) обязана знать
/// РАЗНИЦУ между «ничего не трогали, backend был жив всё время» и «backend
/// реально не отвечал, дождались восстановления» — иначе пользователь видит
/// тревожный тост про перезапуск на процессе, который никто не перезапускал.
enum BackendRestartOutcome: Equatable {
    /// `isBackendAlive()` подтвердил живой backend с первой проверки — ложная
    /// тревога вызывающего (обычно более тугой таймаут пинга у HealthMonitor).
    case alreadyAlive
    /// Backend не ответил первой проверке; дождались восстановления (launchd
    /// respawn в passive, собственный respawn в active).
    case recovered
    /// Восстановить не удалось (лимит попыток в active, таймаут ожидания в passive).
    case failed
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

    /// Счётчик последовательных перезапусков (сбрасывается при успешном ping
    /// или после `restartCooldownSec` тишины между попытками).
    private var consecutiveRestarts = 0
    private static let maxConsecutiveRestarts = 3

    /// Время последней попытки restart — используется для cooldown reset.
    /// Wave 59: даёт supervisor шанс восстановиться после системного memory pressure,
    /// который заглушил backend на > 5 мин (например ночной Jetsam OOM кран).
    private var lastRestartAttemptAt: Date?

    /// Lock для защиты изменяемого состояния в @unchecked Sendable классе.
    private let stateLock = NSLock()

    /// Cooldown: если прошло столько секунд с последней failed попытки,
    /// `consecutiveRestarts` обнуляется и можно снова делать `maxConsecutiveRestarts`
    /// попыток. Тестовый хук позволяет overrid'ить значение для unit тестов.
    private static let defaultRestartCooldownSec: TimeInterval = 300  // 5 минут
    var restartCooldownSec: TimeInterval = BackendSupervisor.defaultRestartCooldownSec

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
#endif

    init(projectRoot: String) {
        self.projectRoot = projectRoot
        self.dataDir = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath
        self.socketPath = (self.dataDir as NSString).appendingPathComponent("krabear.sock")
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
            stateLock.lock()
            let isProcRunning = backendProcess?.isRunning ?? false
            stateLock.unlock()
            guard isProcRunning else { return false }
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
            stateLock.lock()
            consecutiveRestarts = 0
            stateLock.unlock()
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
                    stateLock.lock()
                    consecutiveRestarts = 0
                    stateLock.unlock()
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
                    stateLock.lock()
                    consecutiveRestarts = 0
                    stateLock.unlock()
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
            stateLock.lock()
            consecutiveRestarts = 0
            stateLock.unlock()
            return
        }

        switch supervisionMode {
        case .passive:
            for _ in 0..<100 {  // 100 * 200ms = 20s
                try? await Task.sleep(nanoseconds: 200_000_000)
                if await pingOK() {
                    stateLock.lock()
                    consecutiveRestarts = 0
                    stateLock.unlock()
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
                    stateLock.lock()
                    consecutiveRestarts = 0
                    stateLock.unlock()
                    return
                }
            }
            throw IPCError.socketConnectFailed("backend не ответил после запуска")
        }
    }

    /// Перезапускает backend, если он мёртв. Возвращает true при успешном восстановлении.
    ///
    /// Bool-обёртка над `restartIfDeadDetailed()` — сохраняет существующий контракт
    /// для вызывающих (`main+IPCRecovery.swift`), которым важен только факт «можно
    /// продолжать». Для честного различения ложной тревоги и настоящего события
    /// (тост пользователю) см. `restartIfDeadDetailed()`.
    func restartIfDead() -> Bool {
        restartIfDeadDetailed() != .failed
    }

    /// Перезапускает backend, если он мёртв, и honestly сообщает, что произошло.
    ///
    /// В active режиме ограничен `maxConsecutiveRestarts` попытками подряд,
    /// чтобы не зациклить перезапуски при системном OOM. В passive режиме
    /// полагается на launchd KeepAlive и только ждёт восстановления.
    ///
    /// `.alreadyAlive` — намеренно ОТДЕЛЕНО от `.recovered`. Живой инцидент
    /// 2026-08-03: `HealthMonitor` пингует с таймаутом 2с; под нагрузкой (load
    /// average 40) этот пинг иногда не укладывается, хотя backend отвечает за
    /// 0.2-0.4с. `isBackendAlive()` использует 60-секундный таймаут и почти всегда
    /// подтверждает, что backend жив — процесс никогда не трогали, а вызывающая
    /// сторона до этого фикса показывала тост «Backend перезапущен» на пустом месте
    /// (7062 срабатываний в логе агента, каждые 1-3 минуты под нагрузкой).
    func restartIfDeadDetailed() -> BackendRestartOutcome {
        if isBackendAlive() {
            stateLock.lock()
            consecutiveRestarts = 0
            stateLock.unlock()
            return .alreadyAlive
        }

        switch supervisionMode {
        case .passive:
            // launchd сам respawn'ит. Мы только ждём через ensureBackendRunning.
            // Rate limit не применяем — launchd сам throttle'ит.
            do {
                try ensureBackendRunning()
                return .recovered
            } catch {
                return .failed
            }

        case .active:
            stateLock.lock()
            // Wave 59: cooldown reset — если прошло >= restartCooldownSec с
            // последней попытки, обнуляем счётчик. Backend получает свежий
            // бюджет на maxConsecutiveRestarts попыток после периода тишины.
            if let lastAttempt = lastRestartAttemptAt,
               Date().timeIntervalSince(lastAttempt) >= restartCooldownSec {
                consecutiveRestarts = 0
            }
            guard consecutiveRestarts < Self.maxConsecutiveRestarts else {
                stateLock.unlock()
                return .failed
            }
            consecutiveRestarts += 1
            lastRestartAttemptAt = Date()
            stateLock.unlock()

            stopBackend()
            do {
                try ensureBackendRunning()
                return .recovered
            } catch {
                return .failed
            }
        }
    }

    /// launchd-label backend-сервиса (scripts/install_backend_launchagent.command).
    static let backendLaunchdLabel = "ai.krab.ear.backend"

    /// Путь к точке входа backend-процесса для standalone active-режима.
    ///
    /// S3/Р9: запускаем `main.py`, а НЕ `backend/service.py` напрямую — прямой
    /// запуск делает `service.py` модулем `__main__`, а `rest_server.py`
    /// импортирует `backend.service`, поэтому при включённом in-process REST
    /// файл исполнялся бы ВТОРЫМ экземпляром модуля (два разных класса
    /// `BackendService` в одном процессе). Тот же фикс уже применён к
    /// `ai.krab.ear.backend.plist.template` (passive/launchd-режим) —
    /// standalone-путь имел ровно тот же класс бага. Выделено в чистую
    /// функцию для юнит-тестов (тот же приём, что `kickstartArguments`).
    static func backendScriptPath(projectRoot: String) -> String {
        (projectRoot as NSString).appendingPathComponent("KrabEar/main.py")
    }

    /// Окружение для standalone-спавна backend-процесса (active-режим
    /// супервизора — DMG-получатели без установленного launchd-юнита).
    /// Зеркалит `ai.krab.ear.backend.plist.template`'s `EnvironmentVariables`
    /// для двух полей, критичных для корректности данных:
    /// - `KRAB_EAR_DATA_DIR` обязан совпадать с аргументом `--data-dir`
    ///   (тот же баг класса S3/Р3 — без этого `settings.DATA_DIR` внутри
    ///   backend остаётся дефолтным `~/.krab_ear_data`, а REST/purge/cloud_*
    ///   читают его, пока StateStore получает каталог отдельным аргументом;
    ///   `handle_purge_all_data` чистит НЕ тот каталог, что REST пишет
    ///   `temp_uploads` — сырое аудио переживает "Удалить все данные").
    /// - `PYTHONPATH` обязан включать `KrabEar/`, иначе `import backend.*`/
    ///   `import core.*` внутри `main.py` падает при отсутствующем
    ///   унаследованном окружении (например запуск не из Terminal).
    /// Остальные переменные унаследованы от текущего процесса — standalone
    /// dev-запуск обычно уже идёт из shell с нужным PATH/токенами.
    /// Выделено в чистую функцию для юнит-тестов (тот же приём, что
    /// `backendScriptPath`).
    static func backendEnvironment(
        projectRoot: String,
        dataDir: String,
        base: [String: String] = ProcessInfo.processInfo.environment
    ) -> [String: String] {
        var env = base
        env["KRAB_EAR_DATA_DIR"] = dataDir
        env["PYTHONPATH"] = (projectRoot as NSString).appendingPathComponent("KrabEar")
        return env
    }

    /// Аргументы launchctl для принудительного рестарта launchd-owned backend.
    /// Выделено в чистую функцию для юнит-тестов.
    static func kickstartArguments(uid: uid_t) -> [String] {
        ["kickstart", "-k", "gui/\(uid)/\(backendLaunchdLabel)"]
    }

    /// Принудительный рестарт ЗАВЕДОМО ЖИВОГО backend-процесса (audio-wedge:
    /// жив по IPC, мёртв по аудио — спека 2026-07-15). НЕ смешивать с
    /// restartIfDead(): тот short-circuit'ится на живом процессе, а
    /// stopBackend() в passive-режиме — no-op. В passive это ровно ручной
    /// рецепт живого инцидента 13-07: launchctl kickstart -k.
    func forceRestartBackend() -> Bool {
        switch supervisionMode {
        case .passive:
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            p.arguments = Self.kickstartArguments(uid: getuid())
            do {
                try p.run()
            } catch {
                return false
            }
            p.waitUntilExit()
            return p.terminationStatus == 0
        case .active:
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
            stateLock.lock()
            let proc = backendProcess
            backendProcess = nil
            stateLock.unlock()
            
            if let p = proc {
                p.terminate()
                // Эскалация SIGTERM -> SIGKILL через 1 сек
                DispatchQueue.global(qos: .utility).async {
                    var waitCount = 0
                    while p.isRunning && waitCount < 10 {
                        Thread.sleep(forTimeInterval: 0.1)
                        waitCount += 1
                    }
                    if p.isRunning {
                        kill(p.processIdentifier, SIGKILL)
                    }
                }
            }
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

        let backendScript = Self.backendScriptPath(projectRoot: projectRoot)
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
        process.environment = Self.backendEnvironment(projectRoot: projectRoot, dataDir: dataDir)

        // Нельзя оставлять нечитабельный Pipe: при переполнении буфера backend может зависнуть.
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        try process.run()
        stateLock.lock()
        backendProcess = process
        stateLock.unlock()
    }
}
