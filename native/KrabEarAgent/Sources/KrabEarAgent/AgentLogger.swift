/*
 Локальный файловый логгер нативного агента Krab Ear.

 Связи модуля:
 1) main.swift: пишет диагностику жизненного цикла записи/вставки/истории.
 2) Логи сохраняются в ~/Library/Application Support/KrabEar/agent.log.
*/

import Foundation

/// Потокобезопасный минималистичный логгер агента в отдельный файл.
final class AgentLogger: @unchecked Sendable {
    static let shared = AgentLogger()

    private let queue = DispatchQueue(label: "krabear.agent.logger", qos: .utility)
    private let fileURL: URL
    private let formatter: DateFormatter

    init(dataDirPath: String = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath) {
        let dataDirURL = URL(fileURLWithPath: dataDirPath, isDirectory: true)
        self.fileURL = dataDirURL.appendingPathComponent("agent.log")

        let dateFormatter = DateFormatter()
        dateFormatter.locale = Locale(identifier: "en_US_POSIX")
        dateFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        self.formatter = dateFormatter

        queue.async {
            do {
                try FileManager.default.createDirectory(at: dataDirURL, withIntermediateDirectories: true)
                if !FileManager.default.fileExists(atPath: self.fileURL.path) {
                    FileManager.default.createFile(atPath: self.fileURL.path, contents: nil)
                }
            } catch {
                // Безопасный no-op: отсутствие лога не должно ломать агент.
            }
        }
    }

    func info(_ message: String) {
        write(level: "INFO", message: message)
    }

    func warn(_ message: String) {
        write(level: "WARN", message: message)
    }

    func error(_ message: String) {
        write(level: "ERROR", message: message)
    }

    private func write(level: String, message: String) {
        queue.async {
            let ts = self.formatter.string(from: Date())
            let line = "\(ts) [\(level)] \(message)\n"
            guard let data = line.data(using: .utf8) else { return }

            do {
                let handle = try FileHandle(forWritingTo: self.fileURL)
                defer { try? handle.close() }
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
            } catch {
                // Безопасный no-op: логгер не должен прерывать основной поток.
            }
        }
    }
}
