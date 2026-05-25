/*
 AgentRecoveryLoggerTests — Wave 656.

 Тесты для AgentRecoveryLogger: запись bootstrap-этапов и FATAL строк в
 agent-recovery.log.

 Стратегия (аналогична AgentLoggerTests):
 - Инициализируем AgentRecoveryLogger с temp-каталогом.
 - Синхронизируемся через polling (async write queue).
 - Проверяем содержимое файла напрямую.
*/

import XCTest
@testable import KrabEarAgent

final class AgentRecoveryLoggerTests: XCTestCase {

    // MARK: - Helpers

    private var tmpDir: URL!
    private var logFile: URL!

    override func setUp() {
        super.setUp()
        tmpDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("AgentRecoveryLoggerTests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        logFile = tmpDir.appendingPathComponent("agent-recovery.log")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tmpDir)
        super.tearDown()
    }

    private func waitFor(
        keyword: String,
        timeout: TimeInterval = 3.0
    ) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let text = try? String(contentsOf: logFile, encoding: .utf8),
               text.contains(keyword) { return }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }

    private func logContents() -> String {
        (try? String(contentsOf: logFile, encoding: .utf8)) ?? ""
    }

    // MARK: - Tests

    /// logStage(_:) записывает строку с меткой [RECOVERY] и именем этапа.
    func test_logStage_writesRECOVERYLine() {
        let logger = AgentRecoveryLogger(dataDirPath: tmpDir.path)
        logger.logStage("app_launched")
        waitFor(keyword: "app_launched")
        let text = logContents()
        XCTAssertTrue(text.contains("[RECOVERY]"), "Строка должна содержать метку [RECOVERY]")
        XCTAssertTrue(text.contains("app_launched"), "Строка должна содержать имя этапа")
    }

    /// logStage(_:durationMs:) включает поле duration= когда передан аргумент.
    func test_logStage_withDuration_includesDurationField() {
        let logger = AgentRecoveryLogger(dataDirPath: tmpDir.path)
        logger.logStage("ipc_connect_success", durationMs: 42)
        waitFor(keyword: "duration=42ms")
        let text = logContents()
        XCTAssertTrue(text.contains("duration=42ms"),
                      "Строка должна содержать duration=42ms при передаче durationMs")
    }

    /// logFatal(_:) записывает строку с меткой [FATAL].
    func test_logFatal_writesFATALLine() {
        let logger = AgentRecoveryLogger(dataDirPath: tmpDir.path)
        logger.logFatal("ipc_connect_fail: connection refused")
        waitFor(keyword: "[FATAL]")
        let text = logContents()
        XCTAssertTrue(text.contains("[FATAL]"), "Строка должна содержать метку [FATAL]")
        XCTAssertTrue(text.contains("ipc_connect_fail"),
                      "Строка должна содержать переданное сообщение")
    }
}
