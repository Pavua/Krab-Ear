/*
 Локальный файловый логгер нативного агента Krab Ear.

 Связи модуля:
 1) main.swift: пишет диагностику жизненного цикла записи/вставки/истории.
 2) Логи сохраняются в ~/Library/Application Support/KrabEar/agent.log.

 Архитектура (после fix/agent-logger-resilience):
 - Хранит persistent FileHandle, открытый один раз при инициализации.
 - При ошибке записи: логирует в stderr через NSLog, обнуляет хэндл, делает
   одну попытку переоткрыть + повторить запись (reopen-on-failure).
 - Serial queue гарантирует thread-safety без дополнительных блокировок.
 - Disk-full / permission-denied ловятся явно; агент продолжает работу.
*/

import Foundation

/// Потокобезопасный минималистичный логгер агента в отдельный файл.
final class AgentLogger: @unchecked Sendable {
    static let shared = AgentLogger()

    private let queue = DispatchQueue(label: "krabear.agent.logger", qos: .utility)
    private let fileURL: URL
    private let formatter: DateFormatter

    // Persistent handle — открывается один раз, переоткрывается при сбое.
    private var handle: FileHandle?

    init(dataDirPath: String = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath) {
        let dataDirURL = URL(fileURLWithPath: dataDirPath, isDirectory: true)
        self.fileURL = dataDirURL.appendingPathComponent("agent.log")

        let dateFormatter = DateFormatter()
        dateFormatter.locale = Locale(identifier: "en_US_POSIX")
        dateFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        self.formatter = dateFormatter

        // Создаём директорию и открываем хэндл на serial queue.
        queue.async {
            do {
                try FileManager.default.createDirectory(at: dataDirURL, withIntermediateDirectories: true)
            } catch {
                NSLog("[AgentLogger] createDirectory fail: %@", "\(error)")
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

    func info(_ message: String) {
        write(level: "INFO", message: message)
    }

    func warn(_ message: String) {
        write(level: "WARN", message: message)
    }

    func error(_ message: String) {
        write(level: "ERROR", message: message)
    }

    // MARK: - Private

    /// Открывает (или пересоздаёт) лог-файл и сохраняет persistent handle.
    /// Должна вызываться только внутри serial queue.
    private func openHandle() {
        // Убедимся что файл существует перед открытием хэндла.
        if !FileManager.default.fileExists(atPath: fileURL.path) {
            FileManager.default.createFile(atPath: fileURL.path, contents: nil)
        }
        do {
            let h = try FileHandle(forWritingTo: fileURL)
            try h.seekToEnd()
            self.handle = h
        } catch {
            NSLog("[AgentLogger] openHandle fail (%@): %@", fileURL.lastPathComponent, "\(error)")
            self.handle = nil
        }
    }

    // MARK: - Rotation

    /// Максимальный размер лог-файла до ротации (5 MB).
    private let maxBytes: UInt64 = 5 * 1024 * 1024
    /// Количество хранимых резервных копий после ротации.
    private let backupCount: Int = 3

    /// Ротирует лог, если его размер превышает maxBytes.
    /// Должна вызываться только внутри serial queue.
    private func rotateIfNeeded() {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: fileURL.path),
              let fileSize = attrs[.size] as? UInt64,
              fileSize >= maxBytes else { return }

        // Закрываем текущий хэндл перед переименованием.
        try? handle?.close()
        handle = nil

        let base = fileURL.deletingPathExtension()
        let ext  = fileURL.pathExtension.isEmpty ? "" : ".\(fileURL.pathExtension)"

        // Сдвигаем .1→.2, .2→.3 … и удаляем самый старый.
        for i in stride(from: backupCount - 1, through: 1, by: -1) {
            let src  = URL(fileURLWithPath: "\(base.path).\(i)\(ext)")
            let dest = URL(fileURLWithPath: "\(base.path).\(i + 1)\(ext)")
            if FileManager.default.fileExists(atPath: dest.path) {
                try? FileManager.default.removeItem(at: dest)
            }
            if FileManager.default.fileExists(atPath: src.path) {
                try? FileManager.default.moveItem(at: src, to: dest)
            }
        }
        // .log → .1.log
        let rotated = URL(fileURLWithPath: "\(base.path).1\(ext)")
        if FileManager.default.fileExists(atPath: rotated.path) {
            try? FileManager.default.removeItem(at: rotated)
        }
        try? FileManager.default.moveItem(at: fileURL, to: rotated)

        // Пересоздаём основной файл и открываем хэндл.
        openHandle()
    }

    /// Форматирует и пишет строку лога. Вызывает reopen + одну повторную попытку при сбое.
    private func write(level: String, message: String) {
        queue.async {
            let ts = self.formatter.string(from: Date())
            let line = "\(ts) [\(level)] \(message)\n"
            guard let data = line.data(using: .utf8) else { return }

            // Ленивое открытие: если хэндл ещё не готов — пробуем открыть.
            if self.handle == nil {
                self.openHandle()
            }

            // Проверяем размер перед записью и ротируем при необходимости.
            self.rotateIfNeeded()

            guard let h = self.handle else {
                // Нет хэндла — fallback в stderr, чтобы debug это увидел.
                NSLog("[AgentLogger] write skip (no handle) — [%@] %@", level, message)
                return
            }

            do {
                try h.write(contentsOf: data)
            } catch {
                // Запись упала (disk full, SIGPIPE, stale handle после rebuild…).
                // Сигнализируем в stderr, переоткрываем, одна повторная попытка.
                NSLog("[AgentLogger] write fail (reopening) — [%@] %@: %@", level, message, "\(error)")
                try? h.close()
                self.handle = nil
                self.openHandle()
                if let fresh = self.handle {
                    try? fresh.write(contentsOf: data)
                } else {
                    NSLog("[AgentLogger] reopen failed — message lost: [%@] %@", level, message)
                }
            }
        }
    }
}
