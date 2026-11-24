import XCTest
@testable import KrabEarAgent

final class WakeWordStormBreakerTests: XCTestCase {
    private func fire(_ b: inout WakeWordStormBreaker, at t: TimeInterval) -> (allowed: Bool, tripped: Bool) {
        var tripped = false
        let ok = b.allow(at: t, justTripped: &tripped)
        return (ok, tripped)
    }

    func testRareTriggersAreAllowed() {
        var b = WakeWordStormBreaker()
        for i in 0..<10 { XCTAssertTrue(fire(&b, at: Double(i) * 60).allowed) }
    }

    func testStormTripsAndHolds() {
        var b = WakeWordStormBreaker()
        // срабатывания каждые 3 с — как у krab_ru в живом инциденте
        XCTAssertTrue(fire(&b, at: 0).allowed)
        XCTAssertTrue(fire(&b, at: 3).allowed)
        XCTAssertTrue(fire(&b, at: 6).allowed)
        let fourth = fire(&b, at: 9)
        XCTAssertFalse(fourth.allowed)
        XCTAssertTrue(fourth.tripped)
        // дальше удержание без повторного «tripped»
        let held = fire(&b, at: 12)
        XCTAssertFalse(held.allowed)
        XCTAssertFalse(held.tripped)
        XCTAssertFalse(fire(&b, at: 9 + WakeWordStormBreaker.holdSec - 1).allowed)
    }

    func testRecoversAfterHold() {
        var b = WakeWordStormBreaker()
        for i in 0..<4 { _ = fire(&b, at: Double(i) * 3) }
        XCTAssertTrue(fire(&b, at: 9 + WakeWordStormBreaker.holdSec + 1).allowed)
    }

    func testResetClearsHold() {
        var b = WakeWordStormBreaker()
        for i in 0..<4 { _ = fire(&b, at: Double(i) * 3) }
        XCTAssertFalse(fire(&b, at: 12).allowed)
        b.reset()
        XCTAssertTrue(fire(&b, at: 13).allowed)
    }

    /// reset() только в activate(): в resume()/pause() он обнулял бы размыкатель в цикле «ложное срабатывание → разговор».
    func testBreakerResetOnlyInActivate() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/WakeWordPoller.swift")
        let src = try String(contentsOf: url, encoding: .utf8)
        func body(of signature: String) -> String {
            guard let r = src.range(of: signature) else { return "" }
            let tail = src[r.upperBound...]
            let end = tail.range(of: "\n    }\n")?.lowerBound ?? tail.endIndex
            return String(tail[..<end])
        }
        XCTAssertTrue(body(of: "func activate(").contains("stormBreaker.reset()"))
        XCTAssertFalse(body(of: "func resume(").contains("stormBreaker.reset()"))
        XCTAssertFalse(body(of: "func pause(").contains("stormBreaker.reset()"))
        XCTAssertEqual(src.components(separatedBy: "stormBreaker.reset()").count - 1, 1)
    }
}
