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

    func test_capAfterMaxConsecutive_stopsEscalating() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            XCTAssertTrue(t.shouldEscalate(wedged: true, now: now))
            now += WedgedEscalationTracker.minGapSec
        }
        XCTAssertTrue(t.exhausted)
        // Даже спустя окно — эскалаций больше нет (give-up).
        XCTAssertFalse(t.shouldEscalate(wedged: true, now: now + WedgedEscalationTracker.minGapSec))
    }

    func test_noteHealthy_rearmsCap() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            _ = t.shouldEscalate(wedged: true, now: now)
            now += WedgedEscalationTracker.minGapSec
        }
        XCTAssertTrue(t.exhausted)
        t.noteHealthy()   // реальный чанк захвачен — микрофон жив
        XCTAssertFalse(t.exhausted)
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: now + WedgedEscalationTracker.minGapSec))
    }

    func test_reset_clearsCap() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            _ = t.shouldEscalate(wedged: true, now: now)
            now += WedgedEscalationTracker.minGapSec
        }
        t.reset()
        XCTAssertFalse(t.exhausted)
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: now + 1))
    }
}
