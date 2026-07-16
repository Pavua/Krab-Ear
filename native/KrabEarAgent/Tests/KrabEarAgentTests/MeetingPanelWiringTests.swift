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

    // === Task 3: точки входа — меню-бар + кнопка истории + проводка ===

    func test_menu_item_wired_in_rebuildStatusMenu() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("onMeetingPanelToggle"),
                      "пункт меню «Встреча» обязан быть реально добавлен в rebuildStatusMenu")
    }

    func test_meeting_panel_handler_calls_meeting_start() throws {
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("\"meeting_start\""))
        XCTAssertTrue(src.contains("DispatchQueue.global"),
                      "IPC строго off-main (AGENT-3)")
    }

    func test_toggle_repolls_after_meeting_start_success() throws {
        // Живой смок 2026-07-16: без немедленного re-poll после успешного
        // meeting_start панель ждала бы 15с watchdog'а, чтобы выйти из idle.
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("pollNow()"),
                      "успешный meeting_start обязан немедленно перечитать состояние")
    }

    func test_history_panel_button_routes_to_delegate() throws {
        let src = try source("HistoryPanelController.swift")
        XCTAssertTrue(src.contains("onOpenMeetingPanel") || src.contains("meetingPanelButton"),
                      "кнопка «Встреча» в topActionsRow обязана существовать и роутить в делегат")
    }
}
