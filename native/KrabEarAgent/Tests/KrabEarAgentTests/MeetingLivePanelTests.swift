/*
 MeetingLivePanelTests — C2c Task 1: панель встречи, чистый рендер live-state.

 Прецедент: ConversationStatusOverlayTests (panel headless в тестах безопасен без
 NSApp.run). Task 1 покрывает ТОЛЬКО panel-boilerplate + render(state:) — без IPC/SSE
 (данные придут в Task 2).
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class MeetingLivePanelTests: XCTestCase {

    private func makeState(
        active: Bool = true,
        transcriptTail: String = "обсуждаем релиз ",
        items: [[String: Any]] = [["text": "подготовить документацию", "priority": "high"]],
        decisions: [String] = ["релиз в четверг"],
        questions: [String] = [],
        speakers: [[String: Any]] = [
            ["label": "Спикер 1", "talk_sec": 17.1, "last_active_ts": Date().timeIntervalSince1970],
            ["label": "Спикер 2", "talk_sec": 14.8, "last_active_ts": Date().timeIntervalSince1970 - 95],
        ],
        degradedLLM: Bool = false,
        degradedDiar: Bool = false
    ) -> [String: Any] {
        [
            "ok": true, "active": active,
            "started_at": Date().timeIntervalSince1970 - 120,
            "transcript_len": 640, "transcript_tail": transcriptTail,
            "items": items, "decisions": decisions, "questions": questions,
            "speakers": speakers,
            "degraded": ["llm": degradedLLM, "diarization": degradedDiar],
            "last_updated_ts": Date().timeIntervalSince1970,
        ]
    }

    func test_panel_is_nonactivating_floating_draggable() {
        let c = MeetingLivePanelController()
        XCTAssertEqual(c._testPanelLevel, .floating)
        XCTAssertTrue(c._testPanelIsDraggable)
    }

    func test_render_active_state_populates_sections() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        XCTAssertEqual(c._testSpeakerChipCount, 2)
        XCTAssertEqual(c._testItemRowCount, 2)  // 1 item + 1 decision (questions пусто)
        XCTAssertTrue(c._testTranscriptTailText.contains("обсуждаем релиз"))
        XCTAssertFalse(c._testDegradedBadgeVisible)
        XCTAssertEqual(c._testUIState, .live)
    }

    func test_render_degraded_flags_show_badge() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(degradedDiar: true))
        XCTAssertTrue(c._testDegradedBadgeVisible)
    }

    func test_render_inactive_state_switches_to_idle() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c.render(state: ["ok": true, "active": false])
        XCTAssertEqual(c._testUIState, .idle)
    }

    func test_render_privacy_state() {
        let c = MeetingLivePanelController()
        c.render(state: ["ok": true, "active": false, "privacy_mode_active": true])
        XCTAssertEqual(c._testUIState, .privacy)
    }

    func test_finalizing_state_is_sticky_until_finished() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c.enterFinalizing()
        XCTAssertEqual(c._testUIState, .finalizing)
        // Пока финализация не завершена, обычный active-рендер её НЕ сбивает
        c.render(state: makeState())
        XCTAssertEqual(c._testUIState, .finalizing)
        // inactive (запись остановлена) — тоже остаёмся в finalizing до finished/отчёта
        c.render(state: ["ok": true, "active": false])
        XCTAssertEqual(c._testUIState, .finalizing)
    }

    // === Fable-гейт волны: находки адверсариального ревью ===

    func test_panel_is_closable_and_reusable() {
        // Ревью №1: панель обязана закрываться крестиком и переживать закрытие
        // (isReleasedWhenClosed=false — иначе крэш при повторном show).
        let c = MeetingLivePanelController()
        XCTAssertTrue(c._testPanelStyleMask.contains(.closable))
        XCTAssertFalse(c._testIsReleasedWhenClosed)
        XCTAssertTrue(c._testPanelDelegateIsController)
    }

    func test_window_close_stops_updates_and_timers() {
        // Ревью №1+№4: закрытие панели глушит poll/SSE/header-таймер
        // (сессия backend'а при этом ПРОДОЛЖАЕТСЯ — спека §3).
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        XCTAssertTrue(c._testHeaderTimerActive)
        c._testSimulateWindowWillClose()
        XCTAssertFalse(c._testHeaderTimerActive)
        XCTAssertFalse(c._testPollFallbackActive)
    }

    func test_silence_watchdog_active_during_finalizing() {
        // Ревью №2: потерянный SSE meeting.finished не должен вешать
        // «Финализирую…» навечно — watchdog обязан работать и в .finalizing.
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c.enterFinalizing()
        c._testSimulateSSESilence(seconds: 16)
        XCTAssertTrue(c._testPollFallbackActive)
    }

    func test_finalizing_poll_inactive_delivers_nil_finish() {
        // Ревью №2: poll в finalizing увидел active:false (finished потерян) →
        // deliverFinished(nil) — панель выходит из вечного «Финализирую…».
        let c = MeetingLivePanelController()
        var calls: [String?] = []
        c.onFinished = { calls.append($0) }
        c.render(state: makeState())
        c.enterFinalizing()
        c._testHandlePollState(["ok": true, "active": false])
        XCTAssertEqual(calls.count, 1)
        XCTAssertNil(calls[0])
    }

    func test_sse_flat_payload_without_envelope_is_parsed() {
        // Ревью №3 (test-validates-the-hole): реальный event_bus шлёт ПЛОСКИЙ
        // payload (data: {"speakers":[...]}), без обёртки {type,data} —
        // fallback `?? obj` обязан быть покрыт тестом.
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c._testHandleSSELine("event: meeting.speakers_updated")
        c._testHandleSSELine(#"data: {"speakers":[{"label":"Спикер 1","talk_sec":5.0,"last_active_ts":0}]}"#)
        XCTAssertEqual(c._testSpeakerChipCount, 1)
        c.enterFinalizing()
        var received: [String?] = []
        c.onFinished = { received.append($0) }
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"item_id":"flat-1"}"#)
        XCTAssertEqual(received, ["flat-1"])
    }

    func test_watchdog_polls_while_idle_too() {
        // Живой смок 2026-07-16: панель, открытая ДО завершения meeting_start,
        // залипала в «Встреча не идёт» навсегда — initial poll видел idle,
        // watchdog гейтился на .live, SSE-партиалы состояние не поднимают.
        let c = MeetingLivePanelController()
        c.render(state: ["ok": true, "active": false])  // .idle
        c._testSimulateSSESilence(seconds: 16)
        XCTAssertTrue(c._testPollFallbackActive)
    }

    func test_speaker_chip_shows_staleness() {
        let c = MeetingLivePanelController()
        let old = Date().timeIntervalSince1970 - 200
        c.render(state: makeState(speakers: [["label": "Спикер 1", "talk_sec": 60.0,
                                              "last_active_ts": old]]))
        XCTAssertTrue(c._testSpeakerChipTitles[0].contains("Спикер 1"))
        // возраст данных отображается (точный формат не пинится — только факт наличия «с» или «мин»)
        XCTAssertTrue(c._testSpeakerChipTitles[0].contains("с") || c._testSpeakerChipTitles[0].contains("мин"))
    }

    // === Task 2: SSE/poll/финализация ===

    func test_sse_event_updates_partial_sections() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c._testHandleSSELine("event: meeting.speakers_updated")
        c._testHandleSSELine(#"data: {"type":"meeting.speakers_updated","data":{"speakers":[{"label":"Спикер 1","talk_sec":5.0,"last_active_ts":0}]}}"#)
        XCTAssertEqual(c._testSpeakerChipCount, 1)
    }

    func test_sse_transcript_appended_appends_tail() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(transcriptTail: "начало. "))
        c._testHandleSSELine("event: meeting.transcript_appended")
        c._testHandleSSELine(#"data: {"data":{"chunk_text":"продолжение","total_len":700}}"#)
        XCTAssertTrue(c._testTranscriptTailText.hasSuffix("продолжение "))
    }

    func test_foreign_sse_event_ignored() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        let before = c._testSpeakerChipCount
        c._testHandleSSELine("event: live_subs.result")
        c._testHandleSSELine(#"data: {"data":{"speakers":[]}}"#)
        XCTAssertEqual(c._testSpeakerChipCount, before)
    }

    func test_sse_finished_triggers_report_callback() {
        let c = MeetingLivePanelController()
        var received: String?
        c.onFinished = { itemID in received = itemID }
        c.render(state: makeState())
        c.enterFinalizing()
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"data":{"item_id":"abc-123"}}"#)
        XCTAssertEqual(received, "abc-123")
    }

    func test_finished_delivered_exactly_once() {
        // Fable-гейт: SSE meeting.finished и IPC-ответ meeting_stop оба несут
        // item_id — без one-shot гарда открылись бы ДВА окна отчёта.
        let c = MeetingLivePanelController()
        var calls: [String?] = []
        c.onFinished = { calls.append($0) }
        c.render(state: makeState())
        c.enterFinalizing()
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"data":{"item_id":"abc-123"}}"#)
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"data":{"item_id":"abc-123"}}"#)
        XCTAssertEqual(calls.count, 1)
        // Новая сессия (render active после resetToIdle) взводит гард заново.
        c.resetToIdle()
        c.render(state: makeState())
        c.enterFinalizing()
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"data":{"item_id":"def-456"}}"#)
        XCTAssertEqual(calls.count, 2)
    }

    func test_silence_watchdog_arms_poll_fallback() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c._testSimulateSSESilence(seconds: 16)
        XCTAssertTrue(c._testPollFallbackActive)
        // Любая живая SSE-строка снимает фоллбэк
        c._testHandleSSELine("event: meeting.items_updated")
        c._testHandleSSELine(#"data: {"data":{"items":[],"decisions":[],"questions":[]}}"#)
        XCTAssertFalse(c._testPollFallbackActive)
    }
}
