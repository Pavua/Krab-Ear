import XCTest
@testable import KrabEarAgent

final class WedgedEscalationTrackerTests: XCTestCase {

    func test_notWedged_neverEscalates() {
        var t = WedgedEscalationTracker()
        XCTAssertFalse(t.shouldEscalate(wedged: false, now: 100))
        XCTAssertFalse(t.shouldEscalate(wedged: false, now: 100_000))
    }

    func test_firstWedged_escalates() {
        var t = WedgedEscalationTracker()
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: 100))
    }

    func test_withinGap_suppressed() {
        var t = WedgedEscalationTracker()
        _ = t.shouldEscalate(wedged: true, now: 100)
        XCTAssertFalse(t.shouldEscalate(wedged: true, now: 100 + WedgedEscalationTracker.minGapSec - 1))
    }

    func test_afterGap_escalatesAgain() {
        var t = WedgedEscalationTracker()
        _ = t.shouldEscalate(wedged: true, now: 100)
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: 100 + WedgedEscalationTracker.minGapSec))
    }

    func test_reset_rearms() {
        var t = WedgedEscalationTracker()
        _ = t.shouldEscalate(wedged: true, now: 100)
        t.reset()
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: 101))
    }
}
