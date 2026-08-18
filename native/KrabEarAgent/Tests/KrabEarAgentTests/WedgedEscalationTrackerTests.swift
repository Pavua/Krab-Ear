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
        t.notePoll(running: true, chunkTs: 10.0)
        t.notePoll(running: true, chunkTs: 10.75)
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
        // +1: первый тик только задаёт базу, роста ещё не видно.
        var ts = 500.0
        for _ in 0...WedgedEscalationTracker.minHealthyPolls {
            t.notePoll(running: true, chunkTs: ts)
            ts += 0.75
        }
        XCTAssertFalse(t.exhausted)
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: now + WedgedEscalationTracker.minGapSec))
    }

    func test_notePoll_gap_resets_sustain_counter() {
        var t = WedgedEscalationTracker()
        t.notePoll(running: true, chunkTs: 1.0)
        t.notePoll(running: false, chunkTs: nil)
        t.notePoll(running: true, chunkTs: 2.0)
        XCTAssertEqual(t.consecutiveEscalations, 0)
    }

    /// 🔴 Fable-ревью 2026-08-18: `last_chunk_ts` штампуется только ненулевым
    /// чанком и зануляется лишь в stop()/reinit — при зависании `stream.read()`
    /// (класс 13-07) он ЗАМЕРЗАЕТ non-nil, а тред остаётся жив (running=true).
    /// Проверка «ts != nil» засчитывала такие тики здоровыми и снимала кап за
    /// ~6 с у мёртвого микрофона. Здоровье = РОСТ штампа, а не его наличие.
    func test_frozenChunkTs_neverRearmsCap() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            _ = t.shouldEscalate(wedged: true, now: now)
            now += WedgedEscalationTracker.minGapSec
        }
        XCTAssertTrue(t.exhausted)
        for _ in 0..<(WedgedEscalationTracker.minHealthyPolls * 4) {
            t.notePoll(running: true, chunkTs: 42.0)   // штамп замер
        }
        XCTAssertTrue(t.exhausted, "замороженный last_chunk_ts не считается здоровьем")
    }

    /// Монотонный штамп сбрасывается при рестарте сессии: убывание — не рост.
    func test_decreasingChunkTs_isNotHealthy() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            _ = t.shouldEscalate(wedged: true, now: now)
            now += WedgedEscalationTracker.minGapSec
        }
        var ts = 900.0
        for _ in 0..<(WedgedEscalationTracker.minHealthyPolls * 2) {
            t.notePoll(running: true, chunkTs: ts)
            ts -= 1.0
        }
        XCTAssertTrue(t.exhausted)
    }

    /// nil (backend сбросил сессию) обнуляет и серию, и базу: следующая серия
    /// начинается с нуля, а не досчитывает старую.
    func test_nilChunkTs_resetsStreakAndBaseline() {
        var t = WedgedEscalationTracker()
        var now: TimeInterval = 100
        for _ in 0..<WedgedEscalationTracker.maxConsecutive {
            _ = t.shouldEscalate(wedged: true, now: now)
            now += WedgedEscalationTracker.minGapSec
        }
        var ts = 10.0
        for _ in 0..<(WedgedEscalationTracker.minHealthyPolls - 1) {
            t.notePoll(running: true, chunkTs: ts)
            ts += 0.75
        }
        t.notePoll(running: false, chunkTs: nil)       // разрыв
        t.notePoll(running: true, chunkTs: ts + 0.75)  // только база, роста нет
        XCTAssertTrue(t.exhausted, "серия обязана начаться заново после nil")
    }

    /// running=false при растущем штампе — не здоровье (сессии нет).
    func test_notRunning_isNotHealthy() {
        var t = WedgedEscalationTracker()
        var ts = 5.0
        for _ in 0..<(WedgedEscalationTracker.minHealthyPolls * 2) {
            t.notePoll(running: false, chunkTs: ts)
            ts += 0.75
        }
        XCTAssertEqual(t.consecutiveEscalations, 0)
    }
}
