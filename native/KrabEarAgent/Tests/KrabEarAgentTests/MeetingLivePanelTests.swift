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
        degradedDiar: Bool = false,
        generationToken: String = "Meeting-G1-Opaque"
    ) -> [String: Any] {
        [
            "ok": true, "active": active,
            "generation_token": generationToken,
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

    func test_live_state_preserves_opaque_generation_token() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(
            generationToken: "MiXeD-Token-Without-Normalization"
        ))
        XCTAssertEqual(
            c._testGenerationToken,
            "MiXeD-Token-Without-Normalization"
        )
    }

    func test_start_response_token_is_accepted_before_poll() {
        let c = MeetingLivePanelController()
        c.acceptGenerationToken("Start-Response-Token")
        c.acceptGenerationToken("")

        XCTAssertEqual(c._testGenerationToken, "Start-Response-Token")
    }

    func test_recovery_rejects_token_from_new_start_response() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(generationToken: "Meeting-G1"))
        c._testEnterStopRecovery()

        c.acceptGenerationToken("Meeting-G2")

        XCTAssertTrue(c.hasUnresolvedMeetingStop)
        XCTAssertEqual(c._testGenerationToken, "Meeting-G1")
    }

    func test_recovery_state_is_sticky_and_allows_explicit_retry() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c._testEnterStopRecovery()

        XCTAssertEqual(c._testUIState, .recoveryPending)
        XCTAssertTrue(c._testStopButtonEnabled)
        XCTAssertEqual(c._testGenerationToken, "Meeting-G1-Opaque")

        c.render(state: makeState(transcriptTail: "поздний poll"))
        XCTAssertEqual(c._testUIState, .recoveryPending)
        XCTAssertEqual(c._testGenerationToken, "Meeting-G1-Opaque")
    }

    func test_recovery_poll_inactive_delivers_finish_once() {
        let c = MeetingLivePanelController()
        var calls: [String?] = []
        c.onFinished = { calls.append($0) }
        c.render(state: makeState())
        c._testEnterStopRecovery()

        c._testHandlePollState(["ok": true, "active": false])
        c._testHandlePollState(["ok": true, "active": false])

        XCTAssertEqual(calls.count, 1)
        XCTAssertNil(calls[0])
        XCTAssertNil(c._testGenerationToken)
    }

    func test_older_poll_cannot_reset_newer_live_generation() {
        let c = MeetingLivePanelController()
        var finishedCalls = 0
        c.onFinished = { _ in finishedCalls += 1 }

        let initialPoll = c._testBeginPollRequest()
        c.render(state: makeState(generationToken: "Meeting-G1"))
        let refreshAfterStart = c._testBeginPollRequest()

        c._testHandlePollState(
            ["ok": true, "active": false],
            requestSequence: initialPoll
        )

        XCTAssertEqual(c._testUIState, .live)
        XCTAssertEqual(c._testGenerationToken, "Meeting-G1")
        XCTAssertEqual(finishedCalls, 0)

        c._testHandlePollState(
            makeState(generationToken: "Meeting-G1"),
            requestSequence: refreshAfterStart
        )
        XCTAssertEqual(c._testUIState, .live)
    }

    func test_cancelled_sse_epoch_cannot_finish_new_generation() {
        let c = MeetingLivePanelController()
        var reports: [String?] = []
        c.onFinished = { reports.append($0) }

        let oldStream = c._testBeginSSEStreamEpoch()
        c.render(state: makeState(generationToken: "Meeting-G1"))
        _ = c._testBeginSSEStreamEpoch()
        c.render(state: makeState(generationToken: "Meeting-G2"))

        c._testHandleSSELine(
            "event: meeting.finished",
            streamEpoch: oldStream
        )
        c._testHandleSSELine(
            #"data: {"item_id":"old-report","generation_token":"Meeting-G1"}"#,
            streamEpoch: oldStream
        )

        XCTAssertTrue(reports.isEmpty)
        XCTAssertEqual(c._testUIState, .live)
        XCTAssertEqual(c._testGenerationToken, "Meeting-G2")
    }

    func test_same_sse_stream_rejects_foreign_lifecycle_generation() {
        let c = MeetingLivePanelController()
        var reports: [String?] = []
        c.onFinished = { reports.append($0) }
        c.render(state: makeState(generationToken: "Meeting-G2"))
        let stream = c._testBeginSSEStreamEpoch()

        c._testHandleSSELine(
            "event: meeting.finished",
            streamEpoch: stream
        )
        c._testHandleSSELine(
            #"data: {"item_id":"old-report","generation_token":"Meeting-G1"}"#,
            streamEpoch: stream
        )

        XCTAssertTrue(reports.isEmpty)
        XCTAssertEqual(c._testUIState, .live)
        XCTAssertEqual(c._testGenerationToken, "Meeting-G2")
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

    /// C3b Fable-ревью (2026-07-19, вне скоупа ветки, отдельный chip): `.closable`
    /// во styleMask БЕЗ `.titled` не рендерит НИКАКОЙ видимый крестик закрытия —
    /// живой смок волны C3b подтвердил это на QuickCapturePanelController
    /// (AX — 0 кнопок с subrole AXCloseButton), и та же панель-заготовка
    /// портирована 1-в-1 сюда, значит баг унаследован. `.titled` даёт реальную
    /// нативную кнопку; titlebarAppearsTransparent + titleVisibility=.hidden
    /// убирают саму полосу тайтлбара визуально (безрамочный HUD).
    func test_panel_has_visible_close_button() {
        let c = MeetingLivePanelController()
        XCTAssertTrue(c._testPanelStyleMask.contains(.titled))
        XCTAssertTrue(c._testTitlebarAppearsTransparent)
        XCTAssertEqual(c._testTitleVisibility, .hidden)
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
        c._testHandleSSELine(#"data: {"generation_token":"Meeting-G1-Opaque","speakers":[{"label":"Спикер 1","talk_sec":5.0,"last_active_ts":0}]}"#)
        XCTAssertEqual(c._testSpeakerChipCount, 1)
        c.enterFinalizing()
        var received: [String?] = []
        c.onFinished = { received.append($0) }
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"item_id":"flat-1","generation_token":"Meeting-G1-Opaque"}"#)
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
        c._testHandleSSELine(#"data: {"type":"meeting.speakers_updated","data":{"generation_token":"Meeting-G1-Opaque","speakers":[{"label":"Спикер 1","talk_sec":5.0,"last_active_ts":0}]}}"#)
        XCTAssertEqual(c._testSpeakerChipCount, 1)
    }

    func test_sse_transcript_appended_appends_tail() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(transcriptTail: "начало. "))
        c._testHandleSSELine("event: meeting.transcript_appended")
        c._testHandleSSELine(#"data: {"data":{"generation_token":"Meeting-G1-Opaque","chunk_text":"продолжение","total_len":700}}"#)
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
        c._testHandleSSELine(#"data: {"data":{"item_id":"abc-123","generation_token":"Meeting-G1-Opaque"}}"#)
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
        c._testHandleSSELine(#"data: {"data":{"item_id":"abc-123","generation_token":"Meeting-G1-Opaque"}}"#)
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"data":{"item_id":"abc-123","generation_token":"Meeting-G1-Opaque"}}"#)
        XCTAssertEqual(calls.count, 1)
        // Новая сессия (render active после resetToIdle) взводит гард заново.
        c.resetToIdle()
        c.render(state: makeState())
        c.enterFinalizing()
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"data":{"item_id":"def-456","generation_token":"Meeting-G1-Opaque"}}"#)
        XCTAssertEqual(calls.count, 2)
    }

    func test_silence_watchdog_arms_poll_fallback() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c._testSimulateSSESilence(seconds: 16)
        XCTAssertTrue(c._testPollFallbackActive)
        // Любая живая SSE-строка снимает фоллбэк
        c._testHandleSSELine("event: meeting.items_updated")
        c._testHandleSSELine(#"data: {"data":{"generation_token":"Meeting-G1-Opaque","items":[],"decisions":[],"questions":[]}}"#)
        XCTAssertFalse(c._testPollFallbackActive)
    }

    func test_r2_finished_is_ignored_before_local_token_is_known() {
        let c = MeetingLivePanelController()
        var calls = 0
        c.onFinished = { _ in calls += 1 }

        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(
            #"data: {"item_id":"old","generation_token":"Meeting-G1"}"#
        )

        XCTAssertEqual(calls, 0)
        XCTAssertNil(c.lastDeliveredGenerationToken)
    }

    func test_old_report_completion_cannot_reset_new_generation() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(generationToken: "Meeting-G1"))
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(
            #"data: {"item_id":"old","generation_token":"Meeting-G1"}"#
        )
        XCTAssertEqual(c.lastDeliveredGenerationToken, "Meeting-G1")

        c.acceptGenerationToken("Meeting-G2")

        XCTAssertFalse(
            c.resetToIdleAfterFinished(
                expectedGenerationToken: "Meeting-G1"
            )
        )
        XCTAssertEqual(c._testGenerationToken, "Meeting-G2")
    }

    func test_foreign_partial_events_do_not_mutate_new_generation() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(
            transcriptTail: "текст G2",
            generationToken: "Meeting-G2"
        ))
        let speakersBefore = c._testSpeakerChipCount
        let itemsBefore = c._testItemRowCount

        c._testHandleSSELine("event: meeting.transcript_appended")
        c._testHandleSSELine(
            #"data: {"chunk_text":"старый G1","generation_token":"Meeting-G1"}"#
        )
        c._testHandleSSELine("event: meeting.items_updated")
        c._testHandleSSELine(
            #"data: {"items":[],"decisions":[],"questions":[],"generation_token":"Meeting-G1"}"#
        )
        c._testHandleSSELine("event: meeting.speakers_updated")
        c._testHandleSSELine(
            #"data: {"speakers":[],"generation_token":"Meeting-G1"}"#
        )

        XCTAssertEqual(c._testTranscriptTailText, "текст G2")
        XCTAssertEqual(c._testSpeakerChipCount, speakersBefore)
        XCTAssertEqual(c._testItemRowCount, itemsBefore)
    }
}
