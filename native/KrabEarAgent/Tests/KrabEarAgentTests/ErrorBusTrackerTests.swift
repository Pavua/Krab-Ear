import XCTest
@testable import KrabEarAgent

/// Чистая логика курсора list_recent_errors; без IPC и без UI.
final class ErrorBusTrackerTests: XCTestCase {
    private func payload(_ code: String) -> KrabErrorPayload {
        KrabErrorPayload(
            severity: "warn", component: "audio", code: code,
            message_user: "m", message_debug: "d", timestamp: "t"
        )
    }

    func test_first_poll_is_baseline_and_shows_nothing() {
        let tracker = ErrorBusTracker()
        XCTAssertTrue(tracker.newItemsToShow([payload("old")], latestSeq: 7).isEmpty)
        XCTAssertEqual(tracker.lastSeenSeq, 7)
    }

    func test_new_items_are_shown_and_cursor_advances() {
        let tracker = ErrorBusTracker()
        _ = tracker.newItemsToShow([], latestSeq: 7)
        let shown = tracker.newItemsToShow([payload("a")], latestSeq: 8)
        XCTAssertEqual(shown.map(\.code), ["a"])
        XCTAssertEqual(tracker.lastSeenSeq, 8)
    }

    /// Backend перезапущен: счётчик с нуля. Ошибки новой сессии (rescue при
    /// старте) раньше терялись: курсор ставился в новый latest, а опрос со
    /// старым since_seq возвращал пустой список.
    func test_backend_restart_resets_cursor_so_next_poll_fetches_new_ring() {
        let tracker = ErrorBusTracker()
        _ = tracker.newItemsToShow([], latestSeq: 40)
        XCTAssertTrue(tracker.newItemsToShow([], latestSeq: 1).isEmpty)
        XCTAssertEqual(tracker.lastSeenSeq, 0, "следующий опрос обязан идти с since_seq=0")

        let shown = tracker.newItemsToShow([payload("audio.recording_rescued")], latestSeq: 1)
        XCTAssertEqual(shown.map(\.code), ["audio.recording_rescued"])
        XCTAssertEqual(tracker.lastSeenSeq, 1)
    }

    func test_restart_with_empty_ring_keeps_quiet() {
        let tracker = ErrorBusTracker()
        _ = tracker.newItemsToShow([], latestSeq: 5)
        XCTAssertTrue(tracker.newItemsToShow([], latestSeq: 0).isEmpty)
        XCTAssertTrue(tracker.newItemsToShow([], latestSeq: 0).isEmpty)
        XCTAssertEqual(tracker.lastSeenSeq, 0)
    }
}
