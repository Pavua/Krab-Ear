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
    @MainActor func test_partial_appends_to_live_text() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testIngestPartial("привет мир")
        XCTAssertTrue(c._testLiveText.contains("привет мир"))
    }
    @MainActor func test_notes_list_renders() {
        let c = QuickCapturePanelController()
        c._testSetNotes([["text": "первая заметка", "ts": "2026-07-16T10:00:00"]])
        XCTAssertEqual(c._testNoteRowCount, 1)
    }
}
