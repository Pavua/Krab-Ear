/*
 WakeWordWedgedRestartOrderTests — порядок лечения wedged через wake-word поллер.

 Живой инцидент 2026-11-02: лог «принудительный рестарт backend», затем
 замолчал весь main-thread поллинг — гипотеза: синхронный BackendToast.show
 на main до постановки kickstart в очередь.
*/

import XCTest

final class WakeWordWedgedRestartOrderTests: XCTestCase {

    private func mainSwiftSource() throws -> String {
        var url = URL(fileURLWithPath: #file)
        for _ in 0..<6 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/main.swift")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        XCTFail("не нашли main.swift")
        return ""
    }

    /// Тело колбэка onWedgedEscalation — kickstart обязан идти раньше любого toast.
    func test_onWedgedEscalation_forceRestartBeforeBackendToast() throws {
        let src = try mainSwiftSource()
        guard let start = src.range(of: "onWedgedEscalation:") else {
            XCTFail("onWedgedEscalation не найден в main.swift")
            return
        }
        let tail = src[start.upperBound...]
        guard let end = tail.range(of: "onWedgedGiveUp:") else {
            XCTFail("onWedgedGiveUp не найден после onWedgedEscalation")
            return
        }
        let body = String(tail[..<end.lowerBound])
        guard let restart = body.range(of: "forceRestartBackend"),
              let toast = body.range(of: "BackendToast.shared.show") else {
            XCTFail("в onWedgedEscalation должны быть forceRestartBackend и BackendToast.shared.show")
            return
        }
        XCTAssertLessThan(
            body.distance(from: body.startIndex, to: restart.lowerBound),
            body.distance(from: body.startIndex, to: toast.lowerBound),
            "kickstart (forceRestartBackend) должен инициироваться до BackendToast.show — иначе AppHang на toast блокирует рестарт и поллинг"
        )
    }

    func test_onWedgedEscalation_logs_force_restart_to_agent_recovery() throws {
        let src = try mainSwiftSource()
        guard let start = src.range(of: "onWedgedEscalation:") else {
            XCTFail("onWedgedEscalation не найден в main.swift")
            return
        }
        let tail = src[start.upperBound...]
        guard let end = tail.range(of: "onWedgedGiveUp:") else {
            XCTFail("onWedgedGiveUp не найден после onWedgedEscalation")
            return
        }
        let body = String(tail[..<end.lowerBound])
        XCTAssertTrue(
            body.contains("logForceRestartBackend"),
            "wake-word wedged эскалация должна писать в agent-recovery.log"
        )
        XCTAssertTrue(body.contains("wake_word_wedged"))
    }
}
