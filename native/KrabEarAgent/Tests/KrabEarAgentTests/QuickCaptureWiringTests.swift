/*
 QuickCaptureWiringTests — C3a source-contract тесты (anti test-validates-the-hole,
 паттерн MeetingPanelWiringTests). Грепают РЕАЛЬНЫЙ source, а не поведение
 изолированных юнитов — ловят декоративную проводку (гард определён, но реально
 не подключён) и случайное подключение заметки к paste-пайплайну (спека
 2026-07-16-c3-quick-capture-design.md §2a).
*/

import XCTest

final class QuickCaptureWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        // резолв пути как в MeetingPanelWiringTests (walk-up от #file до Sources/)
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_streamingPaste_guarded_by_quickCapture() throws {
        let src = try source("main+RealtimeOverlay.swift")
        // recordingDidStart обязан быть за гардом quickCaptureActive
        XCTAssertTrue(src.contains("if !quickCaptureActive"),
                      "streaming-paste должен подавляться в режиме заметки")
    }

    func test_quickCapture_never_calls_paste_pipeline() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertFalse(src.contains("handleTranscriptionResult"),
                       "заметка не должна входить в paste-пайплайн")
        XCTAssertFalse(src.contains("pasteToFrontmostApp"))
    }

    func test_dictation_guarded_against_quickCapture() throws {
        let src = try source("main+HotkeyRecording.swift")
        XCTAssertTrue(src.contains("quickCaptureActive"),
                      "Right Option обязан отвергаться при активной заметке")
    }

    func test_meeting_guarded_against_quickCapture() throws {
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("quickCaptureActive"))
    }

    func test_quickCapture_uses_overlay_polling_hooks() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertTrue(src.contains("startRealtimeOverlayPolling()"),
                      "wake-word пауза/оверлей живут в этом хуке — обязателен")
        XCTAssertTrue(src.contains("stopRealtimeOverlayPolling()"))
        XCTAssertTrue(src.contains("set_paste_status"))
        XCTAssertTrue(src.contains("add_to_collection"))
    }
}
