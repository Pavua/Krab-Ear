/*
 QuickCapturePanelTests — C3b Task 1: панель-скретчпад быстрой заметки,
 каркас + чистый рендер (без IPC/сети — данные придут в Task 2).

 Прецедент: MeetingLivePanelTests (panel headless в тестах безопасен без
 NSApp.run).
*/

import XCTest
@testable import KrabEarAgent

final class QuickCapturePanelTests: XCTestCase {
    @MainActor func test_panel_properties() {
        let c = QuickCapturePanelController()
        XCTAssertTrue(c.window?.styleMask.contains(.closable) ?? false)
        XCTAssertFalse(c.window?.isReleasedWhenClosed ?? true)
        XCTAssertEqual(c.window?.level, .floating)
    }
    @MainActor func test_render_idle_and_recording() {
        let c = QuickCapturePanelController()
        c._testSetRecording(false)
        XCTAssertTrue(c._testStatusText.contains("не идёт") || c._testStatusText.contains("Готов"))
        c._testSetRecording(true)
        XCTAssertTrue(c._testHeaderTimerActive)
    }
    @MainActor func test_partial_replaces_live_text() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testIngestPartial("привет мир")
        XCTAssertTrue(c._testLiveText.contains("привет мир"))
    }

    /// realtime.partial_transcript ре-транскрибирует СКОЛЬЗЯЩЕЕ окно последних
    /// buffer_sec секунд аудио (realtime_partial.py) — каждый партиал ЗАМЕНЯЕТ
    /// предыдущий, а не дописывается к нему (иначе на заметке длиннее ~3с
    /// live-текст дублировался бы: "привет привет мир как дела").
    @MainActor func test_second_partial_replaces_first_not_appends() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testIngestPartial("привет")
        c._testIngestPartial("привет мир как дела")
        XCTAssertEqual(c._testLiveText, "привет мир как дела")
    }
    @MainActor func test_notes_list_renders() {
        let c = QuickCapturePanelController()
        c._testSetNotes([["text": "первая заметка", "ts": "2026-07-16T10:00:00"]])
        XCTAssertEqual(c._testNoteRowCount, 1)
    }

    // === C3b Task 2: SSE-партиалы ===

    /// Реальный формат события (event_bus.py::emit + rest_server.py::sse_stream —
    /// `data: json.dumps(event['data'])`) — ПЛОСКИЙ payload, без обёртки {type,data}.
    /// realtime.partial_transcript и realtime.final_transcript оба несут поле "text".
    @MainActor func test_sse_partial_event_ingests_into_live_text() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testHandleSSELine("event: realtime.partial_transcript")
        c._testHandleSSELine(#"data: {"session_id":"s1","text":"привет мир","is_partial":true,"ts":123.0}"#)
        XCTAssertTrue(c._testLiveText.contains("привет мир"))
    }

    @MainActor func test_sse_final_event_ingests_into_live_text() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testHandleSSELine("event: realtime.final_transcript")
        c._testHandleSSELine(#"data: {"session_id":"s1","text":"итоговый текст","is_partial":false,"ts":124.0}"#)
        XCTAssertTrue(c._testLiveText.contains("итоговый текст"))
    }

    /// Толерантность к обёрнутому конверту {type,data:{...}} — фоллбэк `?? obj`
    /// (тот же приём, что MeetingLivePanelController.dispatchSSEEvent).
    @MainActor func test_sse_wrapped_envelope_also_parsed() {
        let c = QuickCapturePanelController()
        c._testHandleSSELine("event: realtime.partial_transcript")
        c._testHandleSSELine(#"data: {"data":{"text":"обёрнутый текст"}}"#)
        XCTAssertTrue(c._testLiveText.contains("обёрнутый текст"))
    }

    /// Событие вне allowlist (например meeting.finished) обязано игнорироваться —
    /// панель-скретчпад не подписана на meeting.*-события.
    @MainActor func test_sse_foreign_event_ignored() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"item_id":"x"}"#)
        XCTAssertEqual(c._testLiveText, "")
    }

    @MainActor func test_sse_empty_text_does_not_append_separator() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testHandleSSELine("event: realtime.partial_transcript")
        c._testHandleSSELine(#"data: {"session_id":"s1","text":"","is_partial":true,"ts":123.0}"#)
        XCTAssertEqual(c._testLiveText, "")
    }

    /// Тот же баг на уровне SSE-потока: два партиала подряд из скользящего
    /// окна не должны наслаиваться друг на друга в отображаемом тексте.
    @MainActor func test_sse_consecutive_partials_replace_not_duplicate() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testHandleSSELine("event: realtime.partial_transcript")
        c._testHandleSSELine(#"data: {"session_id":"s1","text":"hello world","is_partial":true,"ts":123.0}"#)
        c._testHandleSSELine("event: realtime.partial_transcript")
        c._testHandleSSELine(#"data: {"session_id":"s1","text":"hello world how are you","is_partial":true,"ts":126.0}"#)
        XCTAssertEqual(c._testLiveText, "hello world how are you")
    }
}
