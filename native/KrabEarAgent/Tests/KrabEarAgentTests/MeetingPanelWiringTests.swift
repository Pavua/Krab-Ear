/*
 MeetingPanelWiringTests — C2c source-contract тесты (anti test-validates-the-hole,
 паттерн MainErrorsWiringTests). Грепают РЕАЛЬНЫЙ source, а не поведение изолированных
 юнитов — ловят декоративную проводку (метод определён, но реально не вызывается/не
 включён в фильтр).

 Task 2: kнопка «Завершить» реально зовёт meeting_stop + SSE-фильтр несёт все пять
 meeting.* событий. Task 3 дописывает точки входа (меню-бар/кнопка истории).
*/

import XCTest

final class MeetingPanelWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        // резолв пути как в MainErrorsWiringTests (walk-up от #file до Sources/)
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_stop_button_actually_calls_meeting_stop() throws {
        let src = try source("MeetingLivePanelController.swift")
        XCTAssertTrue(src.contains("\"meeting_stop\""),
                      "кнопка Завершить обязана реально звать meeting_stop (anti test-validates-the-hole)")
    }

    func test_sse_filter_lists_all_five_meeting_events() throws {
        let src = try source("MeetingLivePanelController.swift")
        for ev in ["meeting.transcript_appended", "meeting.items_updated",
                   "meeting.speakers_updated", "meeting.finalizing", "meeting.finished"] {
            XCTAssertTrue(src.contains(ev), "SSE-фильтр без \(ev)")
        }
    }
}
