import XCTest
@testable import KrabEarAgent

/// Контракт дебаунса поллинга wake word (spec 2026-07-05):
/// триггер ровно один раз на новую детекцию; первый снапшот — только baseline.
final class WakeWordDetectionTrackerTests: XCTestCase {

    func testFirstPollWithValueBaselinesWithoutTrigger() {
        let t = WakeWordDetectionTracker()
        // Агент перезапустился, а у бэкенда осталась старая детекция —
        // она НЕ должна выстрелить.
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 123.45))
    }

    func testNilThenValueTriggers() {
        let t = WakeWordDetectionTracker()
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: nil))   // baseline: пусто
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))   // появилась — триггер
    }

    func testSameValueDoesNotRetrigger() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 10.0))
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 10.0))
    }

    func testIncreasedValueTriggersAgain() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 11.5))
    }

    func testNilAfterValueDoesNotTriggerUntilNewValue() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        // Бэкенд перезапустился: start() сбросил last_detection в None.
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: nil))
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 3.0)) // новый monotonic-отсчёт
    }

    func testResetRearmsBaseline() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        t.reset()
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 10.0)) // снова baseline
    }
}
