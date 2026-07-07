/*
 RecommendedSetupWiringTests — Задача 5 плана A1 recommended-setup.

 Source-контракт (паттерн test_setupHealthMonitor_is_actually_called_from_startup,
 см. MainHealthMonitorWiringTests.swift): доказывает, что RecommendedSetupStepController
 РЕАЛЬНО встроен в runModelDownloadStepThenComplete(), а не просто определён и
 никогда не вызван (класс бага 2026-07-05: setupErrorBus/setupHealthMonitor).
*/

import XCTest
@testable import KrabEarAgent

final class RecommendedSetupSourceContractTests: XCTestCase {

    func test_runModelDownloadStepThenComplete_invokes_RecommendedSetupStepController() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(
            src.contains("RecommendedSetupStepController("),
            "runModelDownloadStepThenComplete() должен создавать RecommendedSetupStepController " +
            "после ModelDownloadStepController — иначе шаг определён, но никогда не показывается."
        )
    }

    func test_recommended_setup_step_precedes_wake_word_consent_in_source_order() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        guard let recIdx = src.range(of: "RecommendedSetupStepController(")?.lowerBound,
              let wakeIdx = src.range(of: "WakeWordConsentStepController(")?.lowerBound else {
            XCTFail("Оба контроллера должны быть найдены в main.swift")
            return
        }
        XCTAssertTrue(
            recIdx < wakeIdx,
            "RecommendedSetupStepController должен вызываться РАНЬШЕ " +
            "WakeWordConsentStepController в исходном коде (порядок цепочки онбординга)."
        )
    }

    private static var mainSwiftURL: URL {
        let bundleURL = Bundle(for: RecommendedSetupSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/main.swift")
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
            .appendingPathComponent("Sources/KrabEarAgent/main.swift")
    }
}
