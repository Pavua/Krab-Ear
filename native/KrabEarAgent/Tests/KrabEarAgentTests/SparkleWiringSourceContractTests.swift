/*
 SparkleWiringSourceContractTests — source-контракт: Sparkle реально ПОДКЛЮЧЁН
 к lifecycle, а не только определён (класс бага setupErrorBus/setupHealthMonitor,
 2026-07-05: обе функции годами были определены, но не вызваны — фичи мертвы
 в проде при 100% зелёных изолированных тестах).
*/

import XCTest
import Foundation

final class SparkleWiringSourceContractTests: XCTestCase {

    func test_setupSparkleUpdater_is_actually_called_from_startup() throws {
        let src = try String(contentsOf: Self.sourceURL("main.swift"), encoding: .utf8)
        XCTAssertTrue(
            src.contains("setupSparkleUpdater()"),
            "completeStartupAfterBackendReady() должен вызывать setupSparkleUpdater()"
        )
    }

    func test_check_updates_menu_item_exists() throws {
        let src = try String(contentsOf: Self.sourceURL("main+StatusMenu.swift"), encoding: .utf8)
        XCTAssertTrue(
            src.contains("onCheckForUpdates"),
            "rebuildStatusMenu() должен содержать пункт «Проверить обновления…»"
        )
    }

    /// Резолв файла исходников из тест-бандла (паттерн SFSymbolVerificationTests).
    private static func sourceURL(_ name: String) -> URL {
        let bundleURL = Bundle(for: SparkleWiringSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/\(name)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
    }
}
