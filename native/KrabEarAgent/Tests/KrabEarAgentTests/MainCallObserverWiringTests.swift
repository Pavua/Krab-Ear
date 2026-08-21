import XCTest

/// Source-контракт: setupCallObserver реально вызывается из старта агента
/// (класс бага setupErrorBus/setupHealthMonitor — определено, но не вызвано).
final class MainCallObserverWiringTests: XCTestCase {
    private func sourceText(_ file: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        return try String(contentsOf: root.appendingPathComponent(file), encoding: .utf8)
    }

    func test_setupCallObserver_is_actually_called_from_startup() throws {
        let main = try sourceText("main.swift")
        XCTAssertTrue(main.contains("setupCallObserver()"),
                      "setupCallObserver определён, но не вызван из main.swift — декоративная проводка")
    }

    func test_tearDownCallObserver_is_actually_called_from_shutdown() throws {
        let main = try sourceText("main.swift")
        XCTAssertTrue(main.contains("tearDownCallObserver()"))
    }
}
