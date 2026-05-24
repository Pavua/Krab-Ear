/*
 AgentRecoveryLogger.swift — Wave 533 design / Wave 572 implementation.

 Пишет bootstrap-этапы агента в отдельный файл agent-recovery.log
 (~/Library/Application Support/KrabEar/agent-recovery.log).
 Используется для диагностики холодных стартов >20 s, зафиксированных
 в Wave 551: FATAL at 06:41:04 → теперь каждый этап виден с timestamp + ms.

 Дополнительно пишет Sentry breadcrumbs через SentryConfig (no-op без DSN).

 Связи модуля:
 1) main.swift: логирует applicationDidFinishLaunching start / IPC connect / health ping.
 2) SentryConfig: addBreadcrumb для каждого этапа (no-op без DSN).
 3) AgentLogger: не заменяет, а дополняет — отдельный файл recovery.log.
*/

import Foundation

/// Этап bootstrap-процесса агента.
enum RecoveryStage: String {
    /// applicationDidFinishLaunching начал выполняться.
    case launchStart = "launch_start"
    /// Перед попыткой подключиться к IPC сокету.
    case ipcConnectAttempt = "ipc_connect_attempt"
    /// IPC соединение установлено успешно.
    case ipcConnectSuccess = "ipc_connect_success"
    /// IPC соединение не удалось.
    case ipcConnectFailure = "ipc_connect_failure"
    /// Первый успешный health ping к backend.
    case firstHealthPing = "first_health_ping"
}

/// Потокобезопасный логгер bootstrap-этапов агента.
///
/// - Пишет timestamped строки в `agent-recovery.log` в директории KrabEar Application Support.
/// - Вызывает `SentryConfig.recordBreadcrumb` для каждого этапа.
/// - Serial write queue, persistent FileHandle (паттерн из AgentLogger).
final class AgentRecoveryLogger: @unchecked Sendable {
    static let shared = AgentRecoveryLogger()

    private let queue = DispatchQueue(label: "krabear.agent.recovery.logger", qos: .utility)
    private let fileURL: URL
    private let formatter: DateFormatter

    private var handle: FileHandle?

    /// Время старта процесса — для расчёта duration от launch.
    private let processStart: Date

    init(
        dataDirPath: String = NSString(
            string: "~/Library/Application Support/KrabEar"
        ).expandingTildeInPath
    ) {
        self.processStart = Date()
        let dataDirURL = URL(fileURLWithPath: dataDirPath, isDirectory: true)
        self.fileURL = dataDirURL.appendingPathComponent("agent-recovery.log")

        let dateFormatter = DateFormatter()
        dateFormatter.locale = Locale(identifier: "en_US_POSIX")
        dateFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        self.formatter = dateFormatter

        queue.async {
            do {
                try FileManager.default.createDirectory(
                    at: dataDirURL,
                    withIntermediateDirectories: true
                )
            } catch {
                NSLog("[AgentRecoveryLogger] createDirectory fail: %@", "\(error)")
            }
            self.openHandle()
        }
    }

    deinit {
        queue.sync {
            try? self.handle?.close()
            self.handle = nil
        }
    }

    // MARK: - Public API

    /// Convenience: логирует произвольный этап (string-based) с опциональной длительностью в мс.
    ///
    /// Используется когда этап не входит в `RecoveryStage` enum (внешние вызовы,
    /// новые этапы без enum-расширения).
    ///
    /// - Parameters:
    ///   - stage: строковый идентификатор этапа.
    ///   - durationMs: длительность этапа в мс (опционально).
    func logStage(_ stage: String, durationMs: Int? = nil) {
        let now = Date()
        let elapsed = now.timeIntervalSince(processStart)
        let elapsedMs = Int(elapsed * 1000)
        let ts = formatter.string(from: now)
        var line = "\(ts) [RECOVERY] stage=\(stage) elapsed=\(elapsedMs)ms"
        if let dur = durationMs { line += " duration=\(dur)ms" }
        line += "\n"
        writeRaw(line)

        var crumbData: [String: Any] = ["stage": stage, "elapsed_ms": elapsedMs]
        if let dur = durationMs { crumbData["duration_ms"] = dur }
        let data = crumbData
        DispatchQueue.main.async {
            SentryConfig.recordBreadcrumb(
                category: "bootstrap",
                message: stage,
                data: data
            )
        }
    }

    /// Convenience: логирует фатальную ошибку в agent-recovery.log.
    ///
    /// Используется для диагностики FATAL cold-start >20 s (Wave 551).
    ///
    /// - Parameter msg: описание ошибки.
    func logFatal(_ msg: String) {
        let now = Date()
        let elapsed = now.timeIntervalSince(processStart)
        let elapsedMs = Int(elapsed * 1000)
        let ts = formatter.string(from: now)
        let line = "\(ts) [FATAL] elapsed=\(elapsedMs)ms \(msg)\n"
        writeRaw(line)

        let data: [String: Any] = ["elapsed_ms": elapsedMs, "msg": msg]
        DispatchQueue.main.async {
            SentryConfig.recordBreadcrumb(
                category: "bootstrap",
                message: "FATAL: \(msg)",
                data: data
            )
        }
    }

    /// Логирует этап bootstrap с текущим timestamp и длительностью от старта процесса.
    ///
    /// - Parameters:
    ///   - stage: этап bootstrap.
    ///   - detail: опциональная дополнительная информация (socket path, error message и т.д.).
    func record(stage: RecoveryStage, detail: String? = nil) {
        let now = Date()
        let elapsed = now.timeIntervalSince(processStart)
        let elapsedMs = Int(elapsed * 1000)
        let ts = formatter.string(from: now)
        let detailSuffix = detail.map { " detail=\($0)" } ?? ""
        let line = "\(ts) [RECOVERY] stage=\(stage.rawValue) elapsed=\(elapsedMs)ms\(detailSuffix)\n"
        writeRaw(line)

        // Sentry breadcrumb — no-op если SDK не инициализирован.
        var crumbData: [String: Any] = ["stage": stage.rawValue, "elapsed_ms": elapsedMs]
        if let detail { crumbData["detail"] = detail }
        // SentryConfig is @MainActor — dispatch to main for breadcrumb.
        let data = crumbData
        let stageRaw = stage.rawValue
        DispatchQueue.main.async {
            SentryConfig.recordBreadcrumb(
                category: "bootstrap",
                message: stageRaw,
                data: data
            )
        }
    }

    // MARK: - Private

    /// Максимальный размер лог-файла перед ротацией (1 МБ).
    private static let maxLogBytes: UInt64 = 1_048_576

    private func openHandle() {
        // Ротация: если файл существует и превышает maxLogBytes — переименовываем в .1.
        if let attrs = try? FileManager.default.attributesOfItem(atPath: fileURL.path),
           let size = attrs[.size] as? UInt64,
           size > Self.maxLogBytes {
            let rotatedURL = fileURL.deletingPathExtension()
                .appendingPathExtension("1.log")
            try? FileManager.default.removeItem(at: rotatedURL)
            try? FileManager.default.moveItem(at: fileURL, to: rotatedURL)
        }
        if !FileManager.default.fileExists(atPath: fileURL.path) {
            FileManager.default.createFile(atPath: fileURL.path, contents: nil)
        }
        do {
            let h = try FileHandle(forWritingTo: fileURL)
            try h.seekToEnd()
            self.handle = h
        } catch {
            NSLog("[AgentRecoveryLogger] openHandle fail: %@", "\(error)")
            self.handle = nil
        }
    }

    private func writeRaw(_ line: String) {
        queue.async {
            guard let data = line.data(using: .utf8) else { return }

            if self.handle == nil { self.openHandle() }
            guard let h = self.handle else {
                NSLog("[AgentRecoveryLogger] write skip (no handle): %@", line)
                return
            }
            do {
                try h.write(contentsOf: data)
            } catch {
                NSLog("[AgentRecoveryLogger] write fail (reopening): %@", "\(error)")
                try? h.close()
                self.handle = nil
                self.openHandle()
                if let fresh = self.handle {
                    try? fresh.write(contentsOf: data)
                }
            }
        }
    }
}
