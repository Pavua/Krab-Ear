/*
 Локальный файловый логгер нативного агента Krab Ear.

 Связи модуля:
 1) main.swift: пишет диагностику жизненного цикла записи/вставки/истории.
 2) Логи сохраняются в ~/Library/Application Support/KrabEar/agent.log.
 3) XCTest-процессы получают NullAgentLogger через общий AgentLogging-контракт,
    поэтому raw `swift test` не касается пользовательского Application Support.

 Архитектура (после fix/agent-logger-resilience):
 - Хранит persistent FileHandle, открытый один раз при инициализации.
 - При ошибке записи: логирует в stderr через NSLog, обнуляет хэндл, делает
   одну попытку переоткрыть + повторить запись (reopen-on-failure).
 - Serial queue гарантирует thread-safety без дополнительных блокировок.
 - Disk-full / permission-denied ловятся явно; агент продолжает работу.
*/

import Foundation

/// Узкий контракт логирования нужен, чтобы unit-тесты могли полностью отключить
/// файловый побочный эффект, не меняя production-вызовы `AgentLogger.shared`.
protocol AgentLogging: Sendable {
    func info(_ message: String)
    func warn(_ message: String)
    func error(_ message: String)
}

/// No-op реализация для XCTest runner: сообщения намеренно отбрасываются.
/// Это безопаснее временного HOME, потому что работает и для raw `swift test`.
struct NullAgentLogger: AgentLogging {
    static let shared = NullAgentLogger()

    private init() {}

    func info(_ message: String) {}
    func warn(_ message: String) {}
    func error(_ message: String) {}
}

/// Потокобезопасный минималистичный логгер агента в отдельный файл.
final class AgentLogger: AgentLogging, @unchecked Sendable {
    /// Production сохраняет файловый singleton и исторический путь. Только
    /// XCTest runner получает no-op реализацию до первого consumer.
    static let shared: any AgentLogging = AgentLoggerRuntime.makeShared()

    static let defaultDataDirPath = NSString(
        string: "~/Library/Application Support/KrabEar"
    ).expandingTildeInPath

    private let queue = DispatchQueue(label: "krabear.agent.logger", qos: .utility)
    private let fileURL: URL
    private let formatter: DateFormatter

    // Persistent handle — открывается один раз, переоткрывается при сбое.
    private var handle: FileHandle?

    init(dataDirPath: String = AgentLogger.defaultDataDirPath) {
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
        let handleToClose = self.handle
        queue.async {
            try? handleToClose?.close()
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

    /// Дожидается уже поставленных в serial queue записей. Основной код пишет
    /// асинхронно, а тестам и controlled shutdown нужна точная граница перед
    /// удалением временного каталога или закрытием процесса.
    func waitForPendingWrites() {
        queue.sync {}
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

    /// URL N-й резервной копии. Стандартная конвенция (как Python
    /// RotatingFileHandler): индекс добавляется в конец ПОЛНОГО имени —
    /// `agent.log.1`, `agent.log.2`, … — а НЕ перед расширением (`agent.1.log`).
    private func rotatedURL(_ index: Int) -> URL {
        URL(fileURLWithPath: "\(fileURL.path).\(index)")
    }

    /// Ротирует лог, если его размер превышает maxBytes.
    /// Должна вызываться только внутри serial queue.
    private func rotateIfNeeded() {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: fileURL.path),
              let fileSize = attrs[.size] as? UInt64,
              fileSize >= maxBytes else { return }

        // Закрываем текущий хэндл перед переименованием.
        try? handle?.close()
        handle = nil

        // Сдвигаем .1→.2, .2→.3 … и удаляем самый старый.
        for i in stride(from: backupCount - 1, through: 1, by: -1) {
            let src  = rotatedURL(i)
            let dest = rotatedURL(i + 1)
            if FileManager.default.fileExists(atPath: dest.path) {
                try? FileManager.default.removeItem(at: dest)
            }
            if FileManager.default.fileExists(atPath: src.path) {
                try? FileManager.default.moveItem(at: src, to: dest)
            }
        }
        // agent.log → agent.log.1
        let rotated = rotatedURL(1)
        if FileManager.default.fileExists(atPath: rotated.path) {
            try? FileManager.default.removeItem(at: rotated)
        }
        try? FileManager.default.moveItem(at: fileURL, to: rotated)

        // Пересоздаём основной файл и открываем хэндл.
        openHandle()
    }

    /// Переоткрывает хэндл, если файл по пути был удалён или подменён извне.
    ///
    /// На Unix `unlink()` файла с открытым fd НЕ ломает последующие записи —
    /// они тихо уходят в осиротевший (anonymous) inode, который жив пока fd
    /// открыт. Поэтому стратегия reopen-on-write-error не ловит удаление файла:
    /// `write()` не падает, catch-ветка не срабатывает, `agent.log` не
    /// пересоздаётся. Здесь явно сверяем inode пути с inode нашего дескриптора;
    /// при расхождении (или отсутствии файла) переоткрываем — что воссоздаёт файл.
    /// Должна вызываться только внутри serial queue.
    private func reopenIfStale() {
        guard let h = self.handle else { return }
        // Два лёгких syscall'а (без аллокации NSDictionary через attributesOfItem):
        // lstat по пути + fstat по дескриптору. lstat (не stat) — чтобы не ловить
        // конфликт имён со структурой `stat` в Swift/Darwin; для не-симлинка ≡ stat.
        var pathStat = stat()
        let pathOK = lstat(fileURL.path, &pathStat) == 0       // false — файл удалён
        var fdStat = stat()
        let fdOK = fstat(h.fileDescriptor, &fdStat) == 0

        if !pathOK || !fdOK || pathStat.st_ino != fdStat.st_ino {
            // Путь указывает на другой inode (или исчез) → наш хэндл устарел.
            try? h.close()
            self.handle = nil
            self.openHandle()
        }
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

            // Детект внешнего удаления/подмены файла (stale handle на Unix).
            self.reopenIfStale()

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

/// Чистая граница выбора общей реализации. Параметры оставлены инъецируемыми,
/// чтобы обе ветки проверялись без записи в пользовательский каталог.
enum AgentLoggerRuntime {
    static func isUnitTestProcess(
        bundlePath: String = Bundle.main.bundlePath,
        executablePath: String = CommandLine.arguments.first ?? ""
    ) -> Bool {
        let hasXCTestBundle = [bundlePath, executablePath].contains { path in
            URL(fileURLWithPath: path).pathComponents.contains {
                $0.lowercased().hasSuffix(".xctest")
            }
        }
        // SwiftPM на macOS запускает пакет через системный `/usr/bin/xctest`,
        // поэтому test bundle находится в аргументах host-процесса, а не в
        // `Bundle.main`. Имя системного host — стабильная вторая граница.
        let isXCTestHost = URL(fileURLWithPath: executablePath)
            .lastPathComponent
            .lowercased() == "xctest"
        return hasXCTestBundle || isXCTestHost
    }

    static func makeShared(
        bundlePath: String = Bundle.main.bundlePath,
        executablePath: String = CommandLine.arguments.first ?? "",
        dataDirPath: String = AgentLogger.defaultDataDirPath
    ) -> any AgentLogging {
        guard !isUnitTestProcess(
            bundlePath: bundlePath,
            executablePath: executablePath
        ) else {
            return NullAgentLogger.shared
        }
        return AgentLogger(dataDirPath: dataDirPath)
    }
}
