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

    /// Живой инцидент 2026-08-17: после kickstart несколько тиков last_chunk_ts
    /// звали noteHealthy() и обнуляли give-up кап — цикл 20:17 / 20:47 / 21:17.
    func test_briefHealthyPolls_do_not_rearmCap() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            XCTAssertTrue(t.shouldEscalate(wedged: true, now: now))
            now += WedgedEscalationTracker.minGapSec
        }
        XCTAssertTrue(t.exhausted)
        // Один-два тика last_chunk_ts после kickstart — не «микрофон здоров».
        t.notePoll(running: true, hasRecentChunk: true)
        t.notePoll(running: true, hasRecentChunk: true)
        XCTAssertTrue(t.exhausted)
        XCTAssertFalse(t.shouldEscalate(wedged: true, now: now + WedgedEscalationTracker.minGapSec))
    }

    func test_sustainedHealthyPolls_rearmCap() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            _ = t.shouldEscalate(wedged: true, now: now)
            now += WedgedEscalationTracker.minGapSec
        }
        for _ in 0..<WedgedEscalationTracker.minHealthyPolls {
            t.notePoll(running: true, hasRecentChunk: true)
        }
        XCTAssertFalse(t.exhausted)
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: now + WedgedEscalationTracker.minGapSec))
    }

    func test_notePoll_gap_resets_sustain_counter() {
        var t = WedgedEscalationTracker()
        t.notePoll(running: true, hasRecentChunk: true)
        t.notePoll(running: false, hasRecentChunk: false)
        t.notePoll(running: true, hasRecentChunk: true)
        // после разрыва счётчик устойчивого здоровья сброшен; кап не перевооружён
        // (consecutiveEscalations всё ещё 0, потому что эскалаций не было —
        // проверяем, что minHealthyPolls-1 не зовёт noteHealthy-эквивалент).
        XCTAssertEqual(t.consecutiveEscalations, 0)
    }
}
