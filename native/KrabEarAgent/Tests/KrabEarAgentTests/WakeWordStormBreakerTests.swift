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
}
