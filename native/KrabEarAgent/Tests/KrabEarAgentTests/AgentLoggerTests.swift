/*
 AgentLoggerTests — тесты потокобезопасного файлового логгера агента.

 Стратегия:
 - Создаём AgentLogger с временным каталогом (FileManager.temporaryDirectory).
 - Проверяем, что общий logger XCTest-процесса заменён на NullAgentLogger.
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

    /// Raw `swift test` использует `.xctest` bundle либо системный xctest-host:
    /// оба признака отделяют unit-тесты от production `.app` без env-флагов.
    func test_runtimeBoundary_detectsXCTestRunnerOnly() {
        XCTAssertTrue(AgentLoggerRuntime.isUnitTestProcess(
            bundlePath: "/tmp/KrabEarAgentTests.xctest",
            executablePath: "/tmp/KrabEarAgentTests.xctest/Contents/MacOS/KrabEarAgentTests"
        ))
        XCTAssertTrue(AgentLoggerRuntime.isUnitTestProcess(
            bundlePath: "/Applications/Xcode.app/Contents/Developer/usr/bin",
            executablePath: "/Applications/Xcode.app/Contents/Developer/usr/bin/xctest"
        ))
        XCTAssertFalse(AgentLoggerRuntime.isUnitTestProcess(
            bundlePath: "/Applications/Krab Ear.app",
            executablePath: "/Applications/Krab Ear.app/Contents/MacOS/Krab Ear"
        ))
        XCTAssertTrue(
            AgentLoggerRuntime.isUnitTestProcess(),
            "SwiftPM runner не распознан: bundle=\(Bundle.main.bundlePath), " +
            "executable=\(CommandLine.arguments.first ?? "<нет>")"
        )
    }

    /// Общий logger внутри raw `swift test` обязан быть no-op: любой косвенный
    /// вызов из тестируемого объекта не должен открывать пользовательский файл.
    func test_shared_usesNullLoggerInsideXCTestProcess() {
        XCTAssertTrue(AgentLogger.shared is NullAgentLogger)
        AgentLogger.shared.info("unit-test message must stay in memory")
    }

    /// Фабрика unit-test ветки игнорирует даже явно переданный dataDir и не
    /// создаёт `agent.log`, поэтому тест проверяет герметичность безопасно.
    func test_factory_xctestBranchDoesNotCreateLogFile() {
        let logger = AgentLoggerRuntime.makeShared(
            bundlePath: "/tmp/KrabEarAgentTests.xctest",
            executablePath: "/tmp/KrabEarAgentTests.xctest/Contents/MacOS/KrabEarAgentTests",
            dataDirPath: tmpDir.path
        )

        logger.warn("this message must be discarded")
        Thread.sleep(forTimeInterval: 0.2)

        XCTAssertTrue(logger is NullAgentLogger)
        XCTAssertFalse(FileManager.default.fileExists(atPath: logFile.path))
    }

    /// За пределами XCTest фабрика сохраняет прежний файловый logger и путь,
    /// переданный production-конфигурацией.
    func test_factory_appBranchKeepsFileLogger() {
        let logger = AgentLoggerRuntime.makeShared(
            bundlePath: "/Applications/Krab Ear.app",
            executablePath: "/Applications/Krab Ear.app/Contents/MacOS/Krab Ear",
            dataDirPath: tmpDir.path
        )

        logger.info("production branch marker")
        waitForLog()

        XCTAssertTrue(logger is AgentLogger)
        XCTAssertTrue(logContents().contains("production branch marker"))
    }

    // MARK: - Rotation tests

    /// Когда объём лога превышает maxBytes, создаётся agent.log.1.
    func test_rotation_createsBackupFile() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        // Даём логгеру открыть хэндл.
        Thread.sleep(forTimeInterval: 0.3)

        // Записываем ~6 MB — должны превысить лимит 5 MB и вызвать ротацию.
        let bigChunk = String(repeating: "X", count: 512)
        for _ in 0..<12_000 {
            logger.info(bigChunk)
        }
        // Маркер поставлен после всех больших записей в той же serial queue:
        // его появление не даёт tearDown удалить каталог под живым writer.
        let drainMarker = "ROTATION_CREATE_DRAIN_MARKER"
        logger.info(drainMarker)
        logger.waitForPendingWrites()

        let backup = tmpDir.appendingPathComponent("agent.log.1")
        XCTAssertTrue(FileManager.default.fileExists(atPath: backup.path),
                      "agent.log.1 должен появиться после превышения 5 MB")
        XCTAssertTrue(logContents().contains(drainMarker),
                      "Очередь logger должна завершить записи до очистки каталога")
    }

    /// После ротации основной файл agent.log продолжает существовать и принимает записи.
    func test_rotation_mainFileRemainsWritable() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        Thread.sleep(forTimeInterval: 0.3)

        let bigChunk = String(repeating: "Y", count: 512)
        for _ in 0..<12_000 {
            logger.info(bigChunk)
        }
        // Пишем финальный маркер после ротации.
        logger.info("POST_ROTATION_MARKER")
        logger.waitForPendingWrites()
        XCTAssertTrue(FileManager.default.fileExists(atPath: logFile.path),
                      "agent.log должен существовать после ротации")
        let contents = (try? String(contentsOf: logFile, encoding: .utf8)) ?? ""
        XCTAssertTrue(contents.contains("POST_ROTATION_MARKER"),
                      "Новые записи должны попадать в agent.log после ротации")
    }

    /// Резервных файлов не должно быть больше backupCount (3).
    func test_rotation_backupCountNotExceeded() {
        let logger = AgentLogger(dataDirPath: tmpDir.path)
        Thread.sleep(forTimeInterval: 0.3)

        // Пишем ~30 MB, чтобы вызвать несколько ротаций.
        let bigChunk = String(repeating: "Z", count: 512)
        for _ in 0..<60_000 {
            logger.info(bigChunk)
        }
        let drainMarker = "BACKUP_COUNT_DRAIN_MARKER"
        logger.info(drainMarker)
        logger.waitForPendingWrites()

        let backup4 = tmpDir.appendingPathComponent("agent.log.4")
        XCTAssertTrue(logContents().contains(drainMarker),
                      "Очередь logger должна завершить записи до очистки каталога")
        XCTAssertFalse(FileManager.default.fileExists(atPath: backup4.path),
                       "agent.log.4 не должен существовать при backupCount=3")
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
