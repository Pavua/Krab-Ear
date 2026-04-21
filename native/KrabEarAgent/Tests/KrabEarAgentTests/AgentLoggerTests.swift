/*
 AgentLoggerTests — тесты потокобезопасного файлового логгера агента.

 Стратегия:
 - Создаём AgentLogger с временным каталогом (FileManager.temporaryDirectory).
 - После каждого вызова log-метода синхронизируемся через expectation + sleep,
   так как запись происходит асинхронно на внутренней queue.
 - Проверяем содержимое лог-файла напрямую.
*/

import XCTest
@testable import KrabEarAgent

final class AgentLoggerTests: XCTestCase {

    // MARK: - Helpers

    private var tmpDir: URL!
    private var logFile: URL!

    override func setUp() {
        super.setUp()
        tmpDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("AgentLoggerTests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        logFile = tmpDir.appendingPathComponent("agent.log")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tmpDir)
        super.tearDown()
    }

    /// Ждём, пока файл появится и не будет пустым (асинхронная запись).
    private func waitForLog(timeout: TimeInterval = 2.0) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let data = try? Data(contentsOf: logFile), !data.isEmpty { return }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }

    private func logContents() -> String {
        (try? String(contentsOf: logFile, encoding: .utf8)) ?? ""
    }

    // MARK: - Tests

    /// Лог-файл создаётся в указанном каталоге.
    func test_logFileCreated_inDataDir() {
        _ = AgentLogger(dataDirPath: tmpDir.path)
        // Небольшая пауза для async createDirectory
        Thread.sleep(forTimeInterval: 0.2)
        XCTAssertTrue(FileManager.default.fileExists(atPath: logFile.path),
                      "agent.log должен быть создан в dataDirPath")
    }

    /// info() пишет строку с меткой [INFO].
    func test_info_writesINFOLine() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        logger.info("test info message")
        waitForLog()
        let contents = logContents()
        XCTAssertTrue(contents.contains("[INFO]"), "Лог должен содержать [INFO]")
        XCTAssertTrue(contents.contains("test info message"),
                      "Лог должен содержать тело сообщения")
    }

    /// warn() пишет строку с меткой [WARN].
    func test_warn_writesWARNLine() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        logger.warn("something suspicious")
        waitForLog()
        let contents = logContents()
        XCTAssertTrue(contents.contains("[WARN]"), "Лог должен содержать [WARN]")
        XCTAssertTrue(contents.contains("something suspicious"))
    }

    /// error() пишет строку с меткой [ERROR].
    func test_error_writesERRORLine() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        logger.error("fatal failure")
        waitForLog()
        let contents = logContents()
        XCTAssertTrue(contents.contains("[ERROR]"), "Лог должен содержать [ERROR]")
        XCTAssertTrue(contents.contains("fatal failure"))
    }

    /// Формат строки — "yyyy-MM-dd HH:mm:ss.SSS [LEVEL] message\n".
    func test_logLine_hasTimestampAndLevelAndMessage() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        logger.info("hello")
        waitForLog()
        let line = logContents()
        // Timestamp regex: "2026-04-19 21:…"
        let timestampPattern = #"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"#
        let matches = line.range(of: timestampPattern, options: .regularExpression) != nil
        XCTAssertTrue(matches, "Строка лога должна начинаться с timestamp yyyy-MM-dd HH:mm:ss.SSS")
        XCTAssertTrue(line.contains("[INFO] hello"))
    }

    /// Несколько сообщений добавляются в файл, не затирают друг друга.
    func test_multipleLogs_append() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        logger.info("first")
        logger.warn("second")
        logger.error("third")
        // Ждём все три записи
        let deadline = Date().addingTimeInterval(3.0)
        while Date() < deadline {
            let c = logContents()
            if c.contains("[INFO]") && c.contains("[WARN]") && c.contains("[ERROR]") { break }
            Thread.sleep(forTimeInterval: 0.05)
        }
        let contents = logContents()
        XCTAssertTrue(contents.contains("first"),  "Первое сообщение должно быть в логе")
        XCTAssertTrue(contents.contains("second"), "Второе сообщение должно быть в логе")
        XCTAssertTrue(contents.contains("third"),  "Третье сообщение должно быть в логе")
    }

    /// AgentLogger.shared существует и не падает при вызове.
    func test_shared_singleton_accessible() {
        // Просто убеждаемся что shared не nil и не крашится
        let shared = AgentLogger.shared
        XCTAssertNotNil(shared)
        // Не пишем в shared — его logFile живёт в реальном Application Support
    }

    /// После удаления лог-файла (симуляция stale handle) логгер восстанавливается
    /// и продолжает писать без потери следующего сообщения.
    func test_resilience_afterLogFileRemoved() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        // Даём init создать директорию и открыть хэндл.
        Thread.sleep(forTimeInterval: 0.3)

        logger.info("before removal")
        waitForLog()
        XCTAssertTrue(logContents().contains("before removal"))

        // Удаляем файл — симулируем stale handle (аналог пересоздания файла извне).
        try? FileManager.default.removeItem(at: logFile)
        XCTAssertFalse(FileManager.default.fileExists(atPath: logFile.path))

        // Пишем снова — логгер должен пересоздать файл и записать.
        logger.info("after removal")

        let deadline = Date().addingTimeInterval(3.0)
        while Date() < deadline {
            if let c = try? String(contentsOf: logFile, encoding: .utf8),
               c.contains("after removal") { break }
            Thread.sleep(forTimeInterval: 0.05)
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: logFile.path),
                      "Лог-файл должен быть пересоздан после удаления")
        XCTAssertTrue(logContents().contains("after removal"),
                      "Сообщение после восстановления должно быть в файле")
    }
}
